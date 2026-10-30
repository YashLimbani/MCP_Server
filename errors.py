"""Typed errors for the Stack Exchange client.

str(exc) is what the AI model sees, so every message says what happened AND what to try next.
Messages must never contain the API key or full request URLs.
"""
from __future__ import annotations


class StackExchangeError(Exception):
    """Base class for all errors raised by this server."""


class InvalidInput(StackExchangeError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Invalid request: {detail}. Check the parameter values "
            "(for example the site slug, such as 'stackoverflow') and try again."
        )


class NotFound(StackExchangeError):
    """Raised with a ready-made message, e.g. 'Question 123 not found on stackoverflow...'."""


class InvalidKey(StackExchangeError):
    def __init__(self) -> None:
        super().__init__(
            "The Stack Exchange API rejected the API key. Check that STACKEXCHANGE_KEY is "
            "correct, or unset it to use the lower anonymous quota."
        )


class QuotaExhausted(StackExchangeError):
    def __init__(self, authenticated: bool) -> None:
        hint = (
            "Wait until the quota resets (daily, around 00:00 UTC)."
            if authenticated
            else "Set STACKEXCHANGE_KEY to get a much higher daily quota, or wait until the "
            "quota resets (daily, around 00:00 UTC)."
        )
        super().__init__(f"The Stack Exchange daily request quota is exhausted. {hint}")


class RateLimited(StackExchangeError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"Rate limited by Stack Exchange. Retry after about {retry_after} seconds "
            "and avoid sending many requests at once."
        )


class UpstreamError(StackExchangeError):
    def __init__(self, detail: str = "Stack Exchange is unavailable") -> None:
        super().__init__(f"{detail}. This is usually temporary: try again shortly.")
