"""Client tests: no network needed (httpx.MockTransport + a fake clock)."""
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from client import StackExchangeClient
from errors import (
    InvalidInput,
    InvalidKey,
    NotFound,
    QuotaExhausted,
    RateLimited,
    UpstreamError,
)

SAMPLES = Path(__file__).parent.parent / "samples"
SEARCH = json.loads((SAMPLES / "search_advanced.json").read_text())
QUESTION = json.loads((SAMPLES / "question.json").read_text())
ANSWERS = json.loads((SAMPLES / "answers.json").read_text())


# --- helpers ---------------------------------------------------------------------
class FakeTime:
    """Fake clock: sleeping advances time instantly and records the delay."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def ok(payload):
    return httpx.Response(200, json=payload)


def api_error(error_id, name, message, status=400):
    return httpx.Response(
        status, json={"error_id": error_id, "error_name": name, "error_message": message}
    )


def sequence(*responses):
    """Responder that returns the given responses in order (the last one repeats)."""
    items = list(responses)

    def responder(request):
        return items.pop(0) if len(items) > 1 else items[0]

    return responder


def make(responder, **kwargs):
    """Return (client, fake_time, recorded_requests)."""
    t = kwargs.pop("t", None) or FakeTime()
    calls = []

    def handler(request):
        calls.append(request)
        return responder(request)

    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("today", lambda: date(2026, 10, 30))
    client = StackExchangeClient(
        transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock, **kwargs
    )
    return client, t, calls


def run(coro):
    return asyncio.run(coro)


# --- search ----------------------------------------------------------------------
def test_search_sends_expected_params_and_parses():
    client, _, calls = make(lambda r: ok(SEARCH))
    result = run(
        client.search_questions(
            "asyncio sleep", tags=["python", "asyncio"], sort="votes", accepted_only=True, limit=3
        )
    )
    params = calls[0].url.params
    assert calls[0].url.path == "/2.3/search/advanced"
    assert params.get("q") == "asyncio sleep"
    assert params.get("tagged") == "python;asyncio"
    assert params.get("accepted") == "true"
    assert params.get("sort") == "votes"
    assert params.get("pagesize") == "3"
    assert params.get("site") == "stackoverflow"
    assert len(result.results) == 3


def test_optional_params_are_omitted():
    client, _, calls = make(lambda r: ok(SEARCH))
    run(client.search_questions("x"))
    params = calls[0].url.params
    assert "tagged" not in params and "accepted" not in params


# --- auth -------------------------------------------------------------------------
def test_api_key_is_sent_only_when_configured():
    client, _, calls = make(lambda r: ok(SEARCH), api_key="SECRET")
    run(client.search_questions("x"))
    assert calls[0].url.params.get("key") == "SECRET"

    client, _, calls = make(lambda r: ok(SEARCH))
    run(client.search_questions("x"))
    assert "key" not in calls[0].url.params


def test_invalid_key_error_is_actionable_and_never_leaks_the_key():
    responder = lambda r: api_error(400, "bad_parameter", "key is not valid")
    client, _, _ = make(responder, api_key="SECRET")
    with pytest.raises(InvalidKey) as err:
        run(client.search_questions("x"))
    assert "SECRET" not in str(err.value)
    assert "STACKEXCHANGE_KEY" in str(err.value)


def test_bad_parameter_becomes_invalid_input():
    client, _, _ = make(lambda r: api_error(400, "bad_parameter", "site is required"))
    with pytest.raises(InvalidInput) as err:
        run(client.search_questions("x", site="nope"))
    assert "site is required" in str(err.value)


# --- quota and rate limits -----------------------------------------------------------
def test_quota_is_tracked_from_responses():
    client, _, _ = make(lambda r: ok(SEARCH))
    run(client.search_questions("x"))
    assert client.quota.quota_max == 10000
    assert client.quota.quota_remaining == 9990


def test_quota_status_uses_info_call_when_unknown_then_reuses_it():
    client, _, calls = make(lambda r: ok({"items": [], "quota_max": 300, "quota_remaining": 250}))
    status = run(client.quota_status())
    assert calls[0].url.path == "/2.3/info"
    assert (status.quota_remaining, status.quota_max, status.authenticated) == (250, 300, False)
    run(client.quota_status())
    assert len(calls) == 1


def test_quota_status_reports_active_backoff():
    client, _, _ = make(lambda r: ok({"items": [], "quota_max": 300, "quota_remaining": 250, "backoff": 7}))
    status = run(client.quota_status())
    assert status.backoff_seconds_remaining == 7


def test_exhausted_quota_blocks_requests_until_next_utc_day():
    day = {"d": date(2026, 10, 30)}
    client, _, calls = make(
        lambda r: ok({"items": [], "quota_max": 300, "quota_remaining": 0}),
        today=lambda: day["d"],
    )
    run(client.request("/info", {"site": "stackoverflow"}))
    with pytest.raises(QuotaExhausted):
        run(client.request("/search/advanced", {"q": "x"}))
    assert len(calls) == 1  # the second call never hit the network

    day["d"] += timedelta(days=1)
    run(client.request("/search/advanced", {"q": "x"}))
    assert len(calls) == 2


def test_backoff_field_is_respected_before_the_next_request():
    client, t, calls = make(sequence(ok({**SEARCH, "backoff": 5}), ok(SEARCH)))
    run(client.search_questions("first"))
    run(client.search_questions("second"))
    assert 5 in t.sleeps
    assert len(calls) == 2


def test_very_long_backoff_raises_without_sending_a_request():
    client, _, calls = make(lambda r: ok({**SEARCH, "backoff": 100}))
    run(client.search_questions("first"))
    with pytest.raises(RateLimited) as err:
        run(client.search_questions("second"))
    assert err.value.retry_after == 100
    assert len(calls) == 1


def test_short_throttle_violation_is_waited_out_and_retried():
    throttled = api_error(
        502, "throttle_violation", "too many requests, more requests available in 3 seconds"
    )
    client, t, calls = make(sequence(throttled, ok(SEARCH)))
    result = run(client.search_questions("x"))
    assert len(result.results) == 3
    assert len(calls) == 2
    assert 3 in t.sleeps


def test_throttle_violation_with_huge_wait_means_daily_quota_exhausted():
    throttled = api_error(
        502, "throttle_violation", "too many requests, more requests available in 77737 seconds"
    )
    client, _, calls = make(lambda r: throttled)
    with pytest.raises(QuotaExhausted):
        run(client.search_questions("x"))
    assert len(calls) == 1  # no pointless retries


def test_http_429_with_retry_after_header():
    client, _, _ = make(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimited) as err:
        run(client.search_questions("x"))
    assert err.value.retry_after == 120


def test_requests_are_spaced_by_min_interval():
    client, t, _ = make(lambda r: ok(SEARCH), min_interval=0.2)
    run(client.search_questions("a"))
    run(client.search_questions("b"))
    assert any(abs(s - 0.2) < 1e-9 for s in t.sleeps)


# --- retries -----------------------------------------------------------------------
def test_5xx_is_retried_with_exponential_delay():
    client, t, calls = make(sequence(httpx.Response(503), httpx.Response(503), ok(SEARCH)))
    result = run(client.search_questions("x"))
    assert len(result.results) == 3
    assert len(calls) == 3
    assert t.sleeps == [0.5, 1.0]


def test_retries_are_bounded():
    client, _, calls = make(lambda r: httpx.Response(503))
    with pytest.raises(UpstreamError):
        run(client.search_questions("x"))
    assert len(calls) == 3  # first try plus two retries


def test_timeout_becomes_upstream_error_after_retries():
    def boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    client, _, calls = make(boom)
    with pytest.raises(UpstreamError) as err:
        run(client.search_questions("x"))
    assert "in time" in str(err.value)
    assert len(calls) == 3


def test_non_retryable_errors_are_not_retried():
    client, _, calls = make(lambda r: api_error(400, "bad_parameter", "bad site"))
    with pytest.raises(InvalidInput):
        run(client.search_questions("x"))
    assert len(calls) == 1


# --- caching ------------------------------------------------------------------------
def test_identical_requests_are_cached():
    client, _, calls = make(lambda r: ok(SEARCH))
    run(client.search_questions("same"))
    run(client.search_questions("same"))
    assert len(calls) == 1
    run(client.search_questions("different"))
    assert len(calls) == 2


def test_cache_expires_and_can_be_disabled():
    client, t, calls = make(lambda r: ok(SEARCH), cache_ttl=60)
    run(client.search_questions("x"))
    t.now += 61
    run(client.search_questions("x"))
    assert len(calls) == 2

    client, _, calls = make(lambda r: ok(SEARCH), cache_ttl=0)
    run(client.search_questions("x"))
    run(client.search_questions("x"))
    assert len(calls) == 2


def test_errors_are_not_cached():
    client, _, calls = make(sequence(httpx.Response(503), httpx.Response(503), httpx.Response(503), ok(SEARCH)))
    with pytest.raises(UpstreamError):
        run(client.search_questions("x"))
    run(client.search_questions("x"))
    assert len(calls) == 4


# --- get_question ----------------------------------------------------------------------
def question_router(answer_items=None, question=QUESTION):
    answers = {**ANSWERS, "items": ANSWERS["items"] if answer_items is None else answer_items}

    def responder(request):
        path = request.url.path
        if path.endswith("/questions/27435284"):
            return ok(question)
        if path.endswith("/questions/27435284/answers"):
            return ok(answers)
        if path.endswith("/answers/27436735"):
            return ok({**ANSWERS, "items": [ANSWERS["items"][1]]})
        return ok({"items": []})

    return responder


def test_get_question_makes_two_calls_and_puts_accepted_answer_first():
    client, _, calls = make(question_router())
    q = run(client.get_question(27435284))
    assert len(calls) == 2
    assert [a.answer_id for a in q.answers] == [27436735, 52498068]
    paths = sorted(c.url.path for c in calls)
    assert paths == ["/2.3/questions/27435284", "/2.3/questions/27435284/answers"]
    assert all(c.url.params.get("filter") == "withbody" for c in calls)


def test_get_question_fetches_the_accepted_answer_when_it_is_not_top_voted():
    top_only = [ANSWERS["items"][0]]  # the 378-vote answer, which is NOT the accepted one
    client, _, calls = make(question_router(answer_items=top_only))
    q = run(client.get_question(27435284, max_answers=1))
    assert any(c.url.path.endswith("/answers/27436735") for c in calls)
    assert [a.answer_id for a in q.answers] == [27436735]


def test_get_question_without_answers_makes_one_call():
    client, _, calls = make(question_router())
    q = run(client.get_question(27435284, max_answers=0))
    assert len(calls) == 1
    assert q.answers == []


def test_get_question_not_found():
    client, _, _ = make(lambda r: ok({"items": [], "has_more": False}))
    with pytest.raises(NotFound) as err:
        run(client.get_question(999, site="serverfault"))
    assert "999" in str(err.value) and "serverfault" in str(err.value)
