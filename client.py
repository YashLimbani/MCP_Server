"""Async Stack Exchange API client.

Handles authentication (optional app key), rate limits (API backoff plus client-side
spacing), retries, short-lived caching and quota tracking. Failures are raised as typed
exceptions from errors.py whose messages are written for the AI model.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Literal

import httpx

from errors import (
    InvalidInput,
    InvalidKey,
    NotFound,
    QuotaExhausted,
    RateLimited,
    StackExchangeError,
    UpstreamError,
)
from models import (
    DEFAULT_MAX_BODY_CHARS,
    QuestionDetail,
    QuotaInfo,
    QuotaStatus,
    SearchResult,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.stackexchange.com/2.3"
# A throttle error that asks us to wait longer than this means the DAILY quota is used up.
DAILY_QUOTA_THRESHOLD_SECONDS = 600

Sort = Literal["relevance", "votes", "activity", "creation"]


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class StackExchangeClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = API_BASE,
        timeout: float = 10.0,
        max_retries: int = 2,
        retry_base_delay: float = 0.5,
        cache_ttl: float = 300.0,
        min_interval: float = 0.2,
        max_backoff_wait: float = 10.0,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self._key = api_key or None
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._cache_ttl = cache_ttl
        self._min_interval = min_interval
        self._max_backoff_wait = max_backoff_wait
        self._max_body_chars = max_body_chars
        self._sleep = sleep
        self._clock = clock
        self._today = today

        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "stackexchange-mcp/0.1"},
        )
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self._last_request = float("-inf")
        self._backoff_until = float("-inf")
        self._quota_zero_day: date | None = None
        self.quota = QuotaInfo()

    # --- lifecycle ------------------------------------------------------------
    @property
    def authenticated(self) -> bool:
        return self._key is not None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "StackExchangeClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # --- core request pipeline --------------------------------------------------
    async def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET an API method and return the parsed JSON payload (cached briefly)."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        hit = self._cache.get(cache_key)
        if hit and hit[0] > self._clock():
            return hit[1]

        self._check_quota()
        payload = await self._fetch(path, params)

        if self._cache_ttl > 0:
            if len(self._cache) > 256:  # keep memory bounded
                now = self._clock()
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[cache_key] = (self._clock() + self._cache_ttl, payload)
        return payload

    async def _fetch(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last: StackExchangeError | None = None
        for attempt in range(self._max_retries + 1):
            await self._wait_for_backoff()
            await self._throttle()
            try:
                response = await self._http.get(path, params=self._with_key(params))
                return self._interpret(response)
            except RateLimited as exc:
                # The wait (or a final RateLimited if it is too long) happens at the top
                # of the next loop iteration.
                self._backoff_until = max(self._backoff_until, self._clock() + exc.retry_after)
                last = exc
            except UpstreamError as exc:
                last = exc
                await self._pause_before_retry(attempt)
            except httpx.TimeoutException:
                last = UpstreamError("Stack Exchange did not respond in time")
                await self._pause_before_retry(attempt)
            except httpx.TransportError:
                last = UpstreamError("Could not reach the Stack Exchange API")
                await self._pause_before_retry(attempt)
        assert last is not None
        raise last

    async def _pause_before_retry(self, attempt: int) -> None:
        if attempt < self._max_retries:
            delay = self._retry_base_delay * 2**attempt
            log.warning("Retrying Stack Exchange request in %.1fs (attempt %d)", delay, attempt + 1)
            await self._sleep(delay)

    def _with_key(self, params: dict[str, Any]) -> dict[str, Any]:
        return {**params, "key": self._key} if self._key else dict(params)

    async def _throttle(self) -> None:
        """Space requests out so we never burst."""
        async with self._lock:
            wait = self._last_request + self._min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last_request = self._clock()

    async def _wait_for_backoff(self) -> None:
        remaining = self._backoff_until - self._clock()
        if remaining <= 0:
            return
        if remaining > self._max_backoff_wait:
            raise RateLimited(math.ceil(remaining))
        await self._sleep(remaining)

    def _check_quota(self) -> None:
        if self.quota.quota_remaining == 0 and self._quota_zero_day == self._today():
            raise QuotaExhausted(self.authenticated)

    # --- response handling ------------------------------------------------------
    def _interpret(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            self._update_quota(payload)

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("Retry-After", "")
            raise RateLimited(int(retry_after) if retry_after.isdigit() else 60)
        if isinstance(payload, dict) and "error_id" in payload:
            raise self._map_error(payload)
        if status >= 500:
            raise UpstreamError(f"Stack Exchange is temporarily unavailable (HTTP {status})")
        if status >= 400:
            raise StackExchangeError(f"Unexpected response from Stack Exchange (HTTP {status}).")
        if not isinstance(payload, dict):
            raise UpstreamError("Stack Exchange returned an unreadable response")
        return payload

    def _update_quota(self, payload: dict[str, Any]) -> None:
        if "quota_max" in payload:
            self.quota.quota_max = payload["quota_max"]
        if "quota_remaining" in payload:
            self.quota.quota_remaining = payload["quota_remaining"]
            self._quota_zero_day = self._today() if payload["quota_remaining"] == 0 else None
        backoff = payload.get("backoff")
        if isinstance(backoff, int) and backoff > 0:
            self._backoff_until = max(self._backoff_until, self._clock() + backoff)

    def _map_error(self, payload: dict[str, Any]) -> StackExchangeError:
        error_id = payload.get("error_id")
        message = str(payload.get("error_message") or payload.get("error_name") or "unknown error")
        if error_id == 502:  # throttle_violation: too many requests, or daily quota used up
            seconds = self._seconds_to_wait(payload, message)
            if seconds is not None and seconds > DAILY_QUOTA_THRESHOLD_SECONDS:
                return QuotaExhausted(self.authenticated)
            return RateLimited(seconds if seconds is not None else 60)
        if error_id in (500, 503):
            return UpstreamError(f"Stack Exchange reported an internal error ({error_id})")
        if error_id == 405 or (error_id == 400 and "key" in message.lower()):
            return InvalidKey()
        if error_id == 400:
            return InvalidInput(message)
        return StackExchangeError(f"Stack Exchange API error {error_id}: {message}")

    @staticmethod
    def _seconds_to_wait(payload: dict[str, Any], message: str) -> int | None:
        backoff = payload.get("backoff")
        if isinstance(backoff, int) and backoff > 0:
            return backoff
        match = re.search(r"in (\d+) seconds", message)
        return int(match.group(1)) if match else None

    # --- quota ------------------------------------------------------------------
    async def quota_status(self, site: str = "stackoverflow") -> QuotaStatus:
        if self.quota.quota_remaining is None:
            await self.request("/info", {"site": site})  # cheap call that fills in the quota
        remaining = self._backoff_until - self._clock()
        return QuotaStatus(
            quota_remaining=self.quota.quota_remaining,
            quota_max=self.quota.quota_max,
            authenticated=self.authenticated,
            backoff_seconds_remaining=math.ceil(remaining) if remaining > 0 else None,
        )

    # --- high-level operations ----------------------------------------------------
    async def search_questions(
        self,
        query: str,
        tags: list[str] | None = None,
        site: str = "stackoverflow",
        sort: Sort = "relevance",
        accepted_only: bool = False,
        limit: int = 5,
    ) -> SearchResult:
        payload = await self.request(
            "/search/advanced",
            {
                "q": query,
                "tagged": ";".join(tags) if tags else None,
                "accepted": "true" if accepted_only else None,
                "sort": sort,
                "order": "desc",
                "pagesize": limit,
                "site": site,
            },
        )
        return SearchResult.from_api(payload)

    async def get_question(
        self, question_id: int, site: str = "stackoverflow", max_answers: int = 3
    ) -> QuestionDetail:
        question_call = self.request(
            f"/questions/{question_id}", {"site": site, "filter": "withbody"}
        )
        answer_items: list[dict[str, Any]] = []
        if max_answers > 0:
            answers_call = self.request(
                f"/questions/{question_id}/answers",
                {
                    "site": site,
                    "filter": "withbody",
                    "sort": "votes",
                    "order": "desc",
                    "pagesize": max_answers,
                },
            )
            question_payload, answers_payload = await asyncio.gather(question_call, answers_call)
            answer_items = list(answers_payload.get("items", []))
        else:
            question_payload = await question_call

        items = question_payload.get("items", [])
        if not items:
            raise NotFound(
                f"Question {question_id} was not found on {site}. It may have been deleted, "
                "or it may belong to a different Stack Exchange site."
            )
        question = items[0]

        # The accepted answer is not always among the top-voted ones: fetch it separately.
        accepted_id = question.get("accepted_answer_id")
        if max_answers > 0 and accepted_id and accepted_id not in {
            a["answer_id"] for a in answer_items
        }:
            extra = await self.request(
                f"/answers/{accepted_id}", {"site": site, "filter": "withbody"}
            )
            answer_items += extra.get("items", [])

        return QuestionDetail.from_api(
            question, answer_items, max_answers=max_answers, max_body_chars=self._max_body_chars
        )
