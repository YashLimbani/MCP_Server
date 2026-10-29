import json
from pathlib import Path

from models import QuestionDetail

SAMPLES = Path(__file__).parent.parent / "samples"
QUESTION = json.loads((SAMPLES / "question.json").read_text())["items"][0]
ANSWERS = json.loads((SAMPLES / "answers.json").read_text())["items"]


def _detail(**kw):
    return QuestionDetail.from_api(QUESTION, ANSWERS, **kw)


def test_summary_fields_are_carried_over():
    q = _detail()
    assert q.question_id == 27435284
    assert q.title == "multiprocessing vs multithreading vs asyncio"
    assert q.has_accepted_answer is True
    assert q.created == "2014-12-12"
    assert q.license == "CC BY-SA 4.0"


def test_question_body_is_markdown_with_links_and_decoded_quotes():
    body = _detail().body
    assert "[multiprocessing](https://docs.python.org/3/library/multiprocessing.html)" in body
    assert '"recommended one"' in body
    assert "<p>" not in body and "<a " not in body


def test_answers_accepted_first():
    q = _detail()
    assert [a.answer_id for a in q.answers] == [27436735, 52498068]
    assert q.answers[0].is_accepted is True


def test_max_answers_limits_and_counts_the_rest():
    q = _detail(max_answers=1)
    assert [a.answer_id for a in q.answers] == [27436735]  # the accepted one wins the slot
    assert q.answers_not_shown == 12  # answer_count is 13


def test_zero_answers_requested():
    q = _detail(max_answers=0)
    assert q.answers == []
    assert q.answers_not_shown == 13


def test_answer_links_use_the_questions_site():
    for a in _detail().answers:
        assert a.link.startswith("https://stackoverflow.com/a/")


def test_other_site_links_follow_the_question_link():
    question = dict(QUESTION, link="https://serverfault.com/questions/1/x")
    q = QuestionDetail.from_api(question, ANSWERS)
    assert all(a.link.startswith("https://serverfault.com/a/") for a in q.answers)


def test_body_truncation_points_to_the_question_link():
    q = _detail(max_body_chars=100)
    assert q.body.endswith(f"[truncated, see {QUESTION['link']}]")


def test_no_owner_noise_in_output():
    dumped = _detail().model_dump_json()
    for noisy in ("profile_image", "reputation", "accept_rate", "account_id"):
        assert noisy not in dumped
