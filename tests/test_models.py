import json
from pathlib import Path

from models import QuestionSummary, QuotaInfo, SearchResult

SAMPLE = json.loads(
    (Path(__file__).parent.parent / "samples" / "search_advanced.json").read_text()
)


def test_search_result_shape():
    result = SearchResult.from_api(SAMPLE)
    assert len(result.results) == 3
    assert result.has_more is True


def test_owner_noise_is_dropped():
    dumped = SearchResult.from_api(SAMPLE).model_dump_json()
    for noisy in ("profile_image", "reputation", "accept_rate", "account_id", "content_license"):
        assert noisy not in dumped


def test_dates_are_iso():
    first = SearchResult.from_api(SAMPLE).results[0]
    assert first.created == "2014-12-12"
    assert first.last_activity == "2024-11-10"


def test_html_entities_are_decoded():
    third = SearchResult.from_api(SAMPLE).results[2]
    assert third.author == "an offer can't refuse"


def test_accepted_answer_becomes_boolean():
    first = SearchResult.from_api(SAMPLE).results[0]
    assert first.has_accepted_answer is True
    item = dict(SAMPLE["items"][0])
    del item["accepted_answer_id"]
    assert QuestionSummary.from_api(item).has_accepted_answer is False


def test_missing_owner_is_handled():
    item = dict(SAMPLE["items"][0])
    del item["owner"]
    assert QuestionSummary.from_api(item).author is None


def test_quota_parsed_and_backoff_optional():
    quota = QuotaInfo.from_api(SAMPLE)
    assert quota.quota_max == 10000
    assert quota.quota_remaining == 9990
    assert quota.backoff_seconds is None
    assert QuotaInfo.from_api({"backoff": 10}).backoff_seconds == 10