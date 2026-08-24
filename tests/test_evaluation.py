from __future__ import annotations

import json

import pytest
from conftest import FakeProvider

from agent.provider import LLMResponse
from evaluation.baseline import CHUNK_LINES, CHUNK_OVERLAP, chunk_file
from evaluation.dataset import load_questions, summarise
from evaluation.metrics import check_mentions, grade_answer, score_retrieval
from retrieval.locations import Location

# ------------------------------------------------------------------ chunking


def test_chunk_file_splits_into_overlapping_windows():
    source = "\n".join(f"line {i}" for i in range(1, 101))
    chunks = chunk_file("a.py", source)

    assert chunks[0].start_line == 1
    assert chunks[0].end_line == CHUNK_LINES
    # Successive windows step forward by less than a full window, so a
    # definition straddling a boundary still appears whole somewhere.
    assert chunks[1].start_line == CHUNK_LINES - CHUNK_OVERLAP + 1
    assert all(chunk.file == "a.py" for chunk in chunks)


def test_chunk_file_covers_every_line():
    source = "\n".join(f"line {i}" for i in range(1, 101))
    covered = set()
    for chunk in chunk_file("a.py", source):
        covered.update(range(chunk.start_line, chunk.end_line + 1))
    assert covered == set(range(1, 101))


def test_chunk_file_handles_a_short_file():
    chunks = chunk_file("a.py", "one\ntwo\nthree")
    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 3


def test_chunk_file_ignores_empty_and_blank_files():
    assert chunk_file("a.py", "") == []
    assert chunk_file("a.py", "\n\n\n") == []


def test_chunk_location_is_a_range():
    chunk = chunk_file("a.py", "\n".join(str(i) for i in range(1, 51)))[0]
    assert chunk.location == Location("a.py", 1, CHUNK_LINES)


# ------------------------------------------------------------------- dataset


def _write_questions(tmp_path, questions):
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps({"repo_id": "owner/repo", "questions": questions}), encoding="utf-8"
    )
    return path


def test_load_questions(tmp_path):
    path = _write_questions(
        tmp_path,
        [
            {
                "id": "q1",
                "question": "What calls send?",
                "category": "structural",
                "locations": ["src/a.py:10", "src/b.py:20-40"],
                "answer": "Session.send calls it.",
                "must_mention": ["Session.send"],
            }
        ],
    )
    questions = load_questions(path)

    assert len(questions) == 1
    assert questions[0].repo_id == "owner/repo"
    assert questions[0].expected_locations == [
        Location("src/a.py", 10),
        Location("src/b.py", 20, 40),
    ]
    assert questions[0].must_mention == ["Session.send"]


def test_load_questions_rejects_duplicate_ids(tmp_path):
    path = _write_questions(
        tmp_path,
        [
            {"id": "q1", "question": "a", "category": "structural"},
            {"id": "q1", "question": "b", "category": "structural"},
        ],
    )
    with pytest.raises(ValueError, match="Duplicate"):
        load_questions(path)


def test_load_questions_rejects_an_unknown_category(tmp_path):
    path = _write_questions(
        tmp_path, [{"id": "q1", "question": "a", "category": "vibes"}]
    )
    with pytest.raises(ValueError, match="category"):
        load_questions(path)


def test_summarise_counts_by_category(tmp_path):
    path = _write_questions(
        tmp_path,
        [
            {"id": "q1", "question": "a", "category": "structural"},
            {"id": "q2", "question": "b", "category": "conceptual"},
            {"id": "q3", "question": "c", "category": "conceptual"},
        ],
    )
    assert summarise(load_questions(path)) == "3 questions (1 structural, 2 conceptual)"


# ------------------------------------------------------- retrieval scoring


def test_perfect_retrieval():
    expected = [Location("a.py", 10), Location("b.py", 20)]
    score = score_retrieval(expected, expected)
    assert score.recall == 1.0
    assert score.precision == 1.0
    assert score.hit


def test_missed_retrieval():
    score = score_retrieval([Location("z.py", 1)], [Location("a.py", 10)])
    assert score.recall == 0.0
    assert score.precision == 0.0
    assert not score.hit


def test_partial_recall():
    score = score_retrieval(
        [Location("a.py", 10)], [Location("a.py", 10), Location("b.py", 20)]
    )
    assert score.recall == 0.5
    assert score.hit


def test_precision_penalises_retrieving_a_pile_of_irrelevant_code():
    # Both find the answer, but one buries it in nine irrelevant chunks.
    expected = [Location("a.py", 10)]
    focused = score_retrieval([Location("a.py", 10)], expected)
    noisy = score_retrieval(
        [Location("a.py", 10)] + [Location(f"n{i}.py", 1) for i in range(9)], expected
    )

    assert focused.recall == noisy.recall == 1.0
    assert focused.precision == 1.0
    assert noisy.precision == pytest.approx(0.1)


def test_a_chunk_range_counts_as_finding_a_line_inside_it():
    # The baseline retrieves ranges; the graph retrieves points. Both must be
    # able to satisfy the same expectation.
    score = score_retrieval([Location("a.py", 1, 40)], [Location("a.py", 25)])
    assert score.hit


def test_empty_retrieval_scores_zero_not_an_error():
    score = score_retrieval([], [Location("a.py", 10)])
    assert score.recall == 0.0
    assert score.precision == 0.0


def test_a_question_with_no_expected_locations_does_not_divide_by_zero():
    assert score_retrieval([Location("a.py", 1)], []).recall == 0.0


def test_accept_any_gives_full_credit_for_one_valid_evidence_location():
    # "What does X call" is answered either by X's body or by the callee's
    # definition; finding one is not half an answer.
    expected = [Location("api.py", 74), Location("api.py", 24)]
    strict = score_retrieval([Location("api.py", 24)], expected)
    lenient = score_retrieval([Location("api.py", 24)], expected, accept_any=True)

    assert strict.recall == 0.5
    assert lenient.recall == 1.0


def test_accept_any_still_scores_zero_when_nothing_relevant_was_found():
    score = score_retrieval([Location("zzz.py", 1)], [Location("a.py", 1)], accept_any=True)
    assert score.recall == 0.0
    assert not score.hit


def test_enumeration_questions_still_require_every_location():
    # The default: finding one of fifteen subclasses is not a complete answer.
    expected = [Location("e.py", line) for line in (10, 20, 30, 40)]
    score = score_retrieval([Location("e.py", 10)], expected)
    assert score.recall == 0.25


def test_accept_any_defaults_to_false(tmp_path):
    path = _write_questions(
        tmp_path, [{"id": "q1", "question": "a", "category": "structural"}]
    )
    assert load_questions(path)[0].accept_any is False


def test_accept_any_is_read_from_the_question_file(tmp_path):
    path = _write_questions(
        tmp_path,
        [{"id": "q1", "question": "a", "category": "structural", "accept_any": True}],
    )
    assert load_questions(path)[0].accept_any is True


# --------------------------------------------------------- answer grading


def test_check_mentions_is_case_insensitive():
    assert check_mentions("It calls Session.send here.", ["session.send"]) == []
    assert check_mentions("Nothing relevant.", ["Session.send"]) == ["Session.send"]


def test_grade_answer_uses_the_model_verdict():
    provider = FakeProvider(cypher_payloads=[{"verdict": "correct", "reason": "matches"}])
    grade = grade_answer(provider, "q", "reference", "an answer", [])

    assert grade.verdict == "correct"
    assert grade.is_correct


def test_grade_answer_reports_missing_required_mentions():
    provider = FakeProvider(cypher_payloads=[{"verdict": "partial", "reason": "vague"}])
    grade = grade_answer(provider, "q", "reference", "an answer", ["HTTPAdapter"])

    assert grade.missing_mentions == ["HTTPAdapter"]
    assert not grade.is_correct


def test_an_empty_answer_is_wrong_without_calling_the_grader():
    provider = FakeProvider(cypher_payloads=[])  # would IndexError if called
    grade = grade_answer(provider, "q", "reference", "   ", [])
    assert grade.verdict == "wrong"


def test_a_failing_grader_does_not_crash_the_run():
    class BrokenProvider(FakeProvider):
        def generate_json(self, *args, **kwargs):
            raise RuntimeError("grader unavailable")

    grade = grade_answer(BrokenProvider(), "q", "reference", "an answer", [])
    assert grade.verdict == "wrong"
    assert "Grader failed" in grade.reason


def test_grading_records_token_usage_when_asked():
    from agent.provider import Usage

    provider = FakeProvider(cypher_payloads=[{"verdict": "correct", "reason": "ok"}])
    usage = Usage()
    grade_answer(provider, "q", "ref", "answer", [], usage=usage)

    assert usage.calls == 1
    assert isinstance(provider.generate("x"), LLMResponse)
