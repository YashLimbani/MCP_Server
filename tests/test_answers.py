import json
from pathlib import Path

from models import Answer, top_answers

SAMPLE = json.loads(
    (Path(__file__).parent.parent / "samples" / "answers.json").read_text()
)


def _answers(**kw):
    return [Answer.from_api(i, **kw) for i in SAMPLE["items"]]


def test_parses_both_answers():
    answers = _answers()
    assert [a.answer_id for a in answers] == [52498068, 27436735]
    assert [a.is_accepted for a in answers] == [False, True]


def test_owner_noise_dropped_and_dates_iso():
    dumped = _answers()[0].model_dump_json()
    for noisy in ("profile_image", "reputation", "accept_rate", "account_id"):
        assert noisy not in dumped
    assert _answers()[1].created == "2014-12-12"


def test_license_kept_per_answer():
    a, b = _answers()
    assert a.license == "CC BY-SA 4.0"
    assert b.license == "CC BY-SA 3.0"


def test_link_is_built_from_answer_id():
    assert _answers()[1].link == "https://stackoverflow.com/a/27436735"
    other = Answer.from_api(SAMPLE["items"][1], base_url="https://serverfault.com")
    assert other.link == "https://serverfault.com/a/27436735"


def test_body_is_markdown_not_html():
    body = _answers()[0].body
    for tag in ("<p>", "<pre>", "<code>", "<a ", "<li>"):
        assert tag not in body
    assert "## TL;DR" in body
    assert "> We have walked through" in body
    assert "- CPU Bound => Multi Processing" in body
    assert "[asyncio](https://docs.python.org/3/library/asyncio.html)" in body
    assert "---" in body


def test_code_block_survives_with_indentation():
    body = _answers(max_body_chars=5000)[0].body
    assert '```\nif io_bound:\n    if io_very_slow:\n        print("Use Asyncio")' in body


def test_inline_code_and_emphasis_in_second_answer():
    body = _answers()[1].body
    assert "`multiprocessing` *may* be preferred over `threading`" in body
    assert "\n\n`asyncio` (this technique" in body


def test_truncation_adds_marker_with_link():
    a = Answer.from_api(SAMPLE["items"][0], max_body_chars=300)
    assert a.body.endswith("[truncated, see https://stackoverflow.com/a/52498068]")
    assert len(a.body) < 300 + 80


def test_short_answer_is_not_truncated():
    assert "[truncated" not in _answers(max_body_chars=5000)[1].body


def test_top_answers_puts_accepted_first_then_score():
    ordered = top_answers(_answers(), limit=2)
    assert [a.answer_id for a in ordered] == [27436735, 52498068]  # accepted (176) before 378
    assert len(top_answers(_answers(), limit=1)) == 1


def test_missing_owner_gives_none_author():
    item = dict(SAMPLE["items"][0])
    del item["owner"]
    assert Answer.from_api(item).author is None
