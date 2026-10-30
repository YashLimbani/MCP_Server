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


# --- validation and hints ---------------------------------------------------------------
def test_invalid_tags_are_rejected_before_any_request():
    client, _, calls = make(lambda r: ok(SEARCH))
    for bad in (["two words"], ["a;b"], [""]):
        with pytest.raises(InvalidInput):
            run(client.search_questions("x", tags=bad))
    assert calls == []


def test_empty_results_carry_a_helpful_note():
    client, _, _ = make(lambda r: ok({"items": [], "has_more": False}))
    result = run(client.search_questions("zzzz"))
    assert result.results == []
    assert "search_tags" in result.note


def test_normal_results_have_no_note():
    client, _, _ = make(lambda r: ok(SEARCH))
    assert run(client.search_questions("x")).note is None


# --- search_by_error_message --------------------------------------------------------------
def test_error_search_cleans_the_message_and_reports_the_query():
    trace = (
        "Traceback (most recent call last):\n"
        '  File "/home/me/app.py", line 12, in <module>\n'
        "AttributeError: 'NoneType' object has no attribute 'foo'\n"
    )
    client, _, calls = make(lambda r: ok(SEARCH))
    result = run(client.search_by_error_message(trace, tags=["python"], limit=4))
    params = calls[0].url.params
    assert params.get("q") == "AttributeError: 'NoneType' object has no attribute 'foo'"
    assert params.get("sort") == "relevance"
    assert params.get("tagged") == "python"
    assert params.get("pagesize") == "4"
    assert result.cleaned_query == params.get("q")
    assert len(result.results) == 3


def test_error_search_rejects_empty_input():
    client, _, calls = make(lambda r: ok(SEARCH))
    with pytest.raises(InvalidInput):
        run(client.search_by_error_message("   "))
    assert calls == []


# --- tags (real API samples) -----------------------------------------------------------------
TAGS = json.loads((SAMPLES / "tags.json").read_text())
TAG_WIKI = json.loads((SAMPLES / "tag_wiki.json").read_text())
SITES = json.loads((SAMPLES / "sites.json").read_text())
# /tags/{tag}/info returns the same Tag objects as /tags, so reuse the first sample item.
TAG_INFO = {"items": [TAGS["items"][0]], "has_more": False}


def test_search_tags_params_and_parsing():
    client, _, calls = make(lambda r: ok(TAGS))
    result = run(client.search_tags("asyncio", limit=3))
    params = calls[0].url.params
    assert calls[0].url.path == "/2.3/tags"
    assert params.get("inname") == "asyncio"
    assert params.get("sort") == "popular"
    assert params.get("pagesize") == "3"
    assert [t.name for t in result.tags] == ["python-asyncio", "pytest-asyncio", "nest-asyncio"]
    assert result.tags[0].count == 7945
    assert result.tags[0].has_synonyms is True
    assert result.tags[1].has_synonyms is False
    assert result.has_more is True
    assert "is_moderator_only" not in result.model_dump_json()


def tag_router(wiki=TAG_WIKI, info=TAG_INFO):
    def responder(request):
        path = request.url.path
        if path == "/2.3/tags/python-asyncio/info":
            return ok(info)
        if path == "/2.3/tags/python-asyncio/wikis":
            return ok(wiki)
        if path == "/2.3/search/advanced":
            return ok(SEARCH)
        return ok({"items": []})

    return responder


def test_tag_overview_combines_info_wiki_and_top_questions():
    client, _, calls = make(tag_router())
    overview = run(client.get_tag_overview("python-asyncio", top_questions=2))
    assert len(calls) == 3
    assert overview.tag == "python-asyncio"
    assert overview.question_count == 7945
    assert overview.summary == TAG_WIKI["items"][0]["excerpt"]
    assert len(overview.top_questions) == 3  # the sample search returns 3 items
    search_call = next(c for c in calls if c.url.path == "/2.3/search/advanced")
    assert search_call.url.params.get("tagged") == "python-asyncio"
    assert search_call.url.params.get("sort") == "votes"
    assert search_call.url.params.get("pagesize") == "2"
    assert "q" not in search_call.url.params


def test_tag_overview_without_top_questions_makes_two_calls():
    client, _, calls = make(tag_router())
    overview = run(client.get_tag_overview("python-asyncio", top_questions=0))
    assert len(calls) == 2
    assert overview.top_questions == []


def test_tag_without_wiki_has_no_summary():
    client, _, _ = make(tag_router(wiki={"items": [], "has_more": False}))
    overview = run(client.get_tag_overview("python-asyncio", top_questions=0))
    assert overview.summary is None


def test_wiki_excerpt_with_html_is_converted_defensively():
    wiki = {"items": [{"tag_name": "python-asyncio", "excerpt": "An <b>async</b> library &amp; more."}]}
    client, _, _ = make(tag_router(wiki=wiki))
    overview = run(client.get_tag_overview("python-asyncio", top_questions=0))
    assert overview.summary == "An **async** library & more."


def test_unknown_tag_raises_not_found_with_advice():
    client, _, _ = make(lambda r: ok({"items": [], "has_more": False}))
    with pytest.raises(NotFound) as err:
        run(client.get_tag_overview("nope", top_questions=0))
    assert "search_tags" in str(err.value)


def test_special_characters_in_tag_names_are_percent_encoded():
    client, _, calls = make(lambda r: ok({"items": [{"name": "c#", "count": 1}]}))
    run(client.get_tag_overview("c#", top_questions=0))
    assert any(b"/tags/c%23/info" in c.url.raw_path for c in calls)


# --- sites (real API sample) ------------------------------------------------------------------
def extra_site(slug, name, site_type="main_site", audience="for pros &amp; enthusiasts"):
    return {
        "api_site_parameter": slug,
        "name": name,
        "site_url": f"https://{slug}.com",
        "site_type": site_type,
        "audience": audience,
    }


def test_list_sites_filters_meta_pages_through_results_and_caches_for_a_day():
    page1 = {**SITES, "items": SITES["items"] + [extra_site("meta.stackoverflow", "Meta", "meta_site")]}
    page2 = {"items": [extra_site("askubuntu", "Ask Ubuntu")], "has_more": False}
    pages = {"1": page1, "2": page2}
    client, t, calls = make(lambda r: ok(pages[r.url.params.get("page")]), cache_ttl=300)

    sites = run(client.list_sites())
    assert [s.slug for s in sites] == ["stackoverflow", "serverfault", "superuser", "askubuntu"]
    assert sites[0].name == "Stack Overflow"
    assert sites[0].url == "https://stackoverflow.com"
    assert sites[0].audience == "professional and enthusiast programmers"
    assert sites[3].audience == "for pros & enthusiasts"  # HTML entities are decoded
    assert "styling" not in sites[0].model_dump_json()
    assert len(calls) == 2

    t.now += 3600  # an hour later: still served from the long-lived cache
    run(client.list_sites())
    assert len(calls) == 2
    t.now += 25 * 3600
    run(client.list_sites())
    assert len(calls) == 4
