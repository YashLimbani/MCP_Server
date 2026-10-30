"""Turn a pasted error message or stack trace into a good search query."""
from __future__ import annotations

import re

MAX_QUERY_CHARS = 200

# A line that looks like the actual error (as opposed to stack frames).
_ERROR_LINE = re.compile(
    r"(?:\b[\w.]*(?:Error|Exception)\b|\berror\b|\bfatal\b|\bpanic\b|\bfailed\b)", re.IGNORECASE
)
# Stack-frame noise: "at foo (file:1:2)", 'File "x.py", line 3', "... 12 more", carets.
_SKIP_LINE = re.compile(r"^(?:at\s|File\s|Traceback\b|\.\.\.|[\^~\s]+$)")
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+)+\.\w+(?::\d+(?::\d+)?)?")
_BARE_FILE_LINE = re.compile(r"\b[\w\-]+\.\w{1,5}:\d+(?::\d+)?:?")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LINE_REF = re.compile(r",?\s*\bline \d+\b", re.IGNORECASE)
_THREAD_PREFIX = re.compile(r'^Exception in thread "[^"]*"\s*')
_EMPTY_QUOTES = re.compile(r"""(['"`])\s*\1""")


def clean_error_message(text: str) -> str:
    """Pick the most informative line and strip paths, line numbers and addresses.

    Paths and addresses differ for every user, so they only hurt search relevance.
    Returns '' for empty input.
    """
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    candidates = [ln for ln in lines if not _SKIP_LINE.match(ln)] or lines
    errors = [ln for ln in candidates if _ERROR_LINE.search(ln)]
    chosen = errors[-1] if errors else candidates[0]  # last error line = root cause

    msg = re.sub(r"^Caused by:\s*", "", chosen)
    msg = _THREAD_PREFIX.sub("", msg)
    for pattern in (_PATH, _BARE_FILE_LINE, _HEX, _LINE_REF):
        msg = pattern.sub("", msg)
    msg = _EMPTY_QUOTES.sub("", msg)
    msg = re.sub(r"\s+", " ", msg).strip(" \t:-,;")
    if len(msg) > 1 and msg[0] == msg[-1] and msg[0] in "\"'`":  # whole message wrapped in quotes
        msg = msg[1:-1].strip()

    if len(msg) > MAX_QUERY_CHARS:
        msg = msg[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return msg
