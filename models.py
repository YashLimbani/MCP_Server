"""Trimmed models for Stack Exchange API responses.

Raw API payloads are noisy (owner profile data, edit timestamps, license per item).
These models keep only what an AI assistant needs and convert it to readable form.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

from htmltext import html_to_markdown, truncate_text

DEFAULT_BASE_URL = "https://stackoverflow.com"
DEFAULT_MAX_BODY_CHARS = 2000


def iso_date(ts: int) -> str:
    """Unix timestamp (seconds) -> 'YYYY-MM-DD' in UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _author(item: dict[str, Any]) -> str | None:
    owner = item.get("owner") or {}  # owner can be missing (e.g. deleted users)
    name = owner.get("display_name")
    return html.unescape(name) if name else None


class QuestionSummary(BaseModel):
    question_id: int
    title: str
    link: str
    score: int
    answer_count: int
    view_count: int
    has_accepted_answer: bool
    tags: list[str]
    author: str | None
    created: str
    last_activity: str

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "QuestionSummary":
        return cls(
            question_id=item["question_id"],
            title=html.unescape(item["title"]),
            link=item["link"],
            score=item.get("score", 0),
            answer_count=item.get("answer_count", 0),
            view_count=item.get("view_count", 0),
            has_accepted_answer="accepted_answer_id" in item,
            tags=item.get("tags", []),
            author=_author(item),
            created=iso_date(item["creation_date"]),
            last_activity=iso_date(item["last_activity_date"]),
        )


class SearchResult(BaseModel):
    results: list[QuestionSummary]
    has_more: bool

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "SearchResult":
        return cls(
            results=[QuestionSummary.from_api(i) for i in payload.get("items", [])],
            has_more=payload.get("has_more", False),
        )


class Answer(BaseModel):
    answer_id: int
    question_id: int
    score: int
    is_accepted: bool
    author: str | None
    body: str  # Markdown, truncated
    link: str
    license: str | None  # CC BY-SA 3.0 or 4.0 depending on the post; needed for attribution
    created: str
    last_activity: str

    @classmethod
    def from_api(
        cls,
        item: dict[str, Any],
        base_url: str = DEFAULT_BASE_URL,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> "Answer":
        # The answers endpoint doesn't return a link, so build the short share link.
        link = f"{base_url}/a/{item['answer_id']}"
        body = truncate_text(html_to_markdown(item.get("body", "")), max_body_chars, link)
        return cls(
            answer_id=item["answer_id"],
            question_id=item["question_id"],
            score=item.get("score", 0),
            is_accepted=item.get("is_accepted", False),
            author=_author(item),
            body=body,
            link=link,
            license=item.get("content_license"),
            created=iso_date(item["creation_date"]),
            last_activity=iso_date(item["last_activity_date"]),
        )


def top_answers(answers: list[Answer], limit: int = 3) -> list[Answer]:
    """Accepted answer first, then by score (highest first)."""
    ordered = sorted(answers, key=lambda a: (not a.is_accepted, -a.score))
    return ordered[:limit]


def _base_url(link: str) -> str:
    """'https://serverfault.com/questions/1/x' -> 'https://serverfault.com'."""
    parts = urlparse(link)
    return f"{parts.scheme}://{parts.netloc}"


class QuestionDetail(QuestionSummary):
    """A question with its body and its best answers: the output of get_question."""

    license: str | None
    body: str  # Markdown, truncated
    answers: list[Answer]
    answers_not_shown: int  # how many answers exist beyond the ones included

    @classmethod
    def from_api(  # type: ignore[override]
        cls,
        question_item: dict[str, Any],
        answer_items: list[dict[str, Any]],
        max_answers: int = 3,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> "QuestionDetail":
        base = QuestionSummary.from_api(question_item)
        site_url = _base_url(base.link)  # answer links follow the question's site
        body = truncate_text(
            html_to_markdown(question_item.get("body", "")), max_body_chars, base.link
        )
        answers = top_answers(
            [Answer.from_api(a, site_url, max_body_chars) for a in answer_items],
            max_answers,
        )
        return cls(
            **base.model_dump(),
            license=question_item.get("content_license"),
            body=body,
            answers=answers,
            answers_not_shown=max(base.answer_count - len(answers), 0),
        )


class QuotaInfo(BaseModel):
    """Tracked internally from every response; exposed via get_quota_status."""

    quota_max: int | None = None
    quota_remaining: int | None = None
    backoff_seconds: int | None = None  # only present when the API asks us to slow down

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "QuotaInfo":
        return cls(
            quota_max=payload.get("quota_max"),
            quota_remaining=payload.get("quota_remaining"),
            backoff_seconds=payload.get("backoff"),
        )


class QuotaStatus(BaseModel):
    """Output of get_quota_status."""

    quota_remaining: int | None
    quota_max: int | None
    authenticated: bool  # True when STACKEXCHANGE_KEY is set
    backoff_seconds_remaining: int | None  # None when no backoff is active
