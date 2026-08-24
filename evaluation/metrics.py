"""Scoring: did retrieval find the right code, and is the answer correct.

Two independent measures, deliberately kept apart:

* **Retrieval** — of the locations that genuinely answer the question, how many
  did the system put in front of the model? This is mechanical and needs no
  LLM, so it cannot drift.
* **Answer correctness** — graded by an LLM against a reference answer written
  by hand from the source. Kept separate because a system can retrieve the
  right code and still answer badly, and the distinction is the interesting
  part of the result.

Phase 3 scored a system as "working" when its query returned rows. That is not
the same as answering the question, and one of the answers it counted was the
hedge "it calls another function whose name contains 'cookie'". These metrics
exist to close that gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent.provider import LLMProvider, Usage
from retrieval.locations import Location

Verdict = Literal["correct", "partial", "wrong"]

GRADER_MAX_TOKENS = 700

_GRADER_SYSTEM = (
    "You grade answers about a Python codebase against a reference answer.\n"
    "Judge only whether the answer is factually correct and responsive to the "
    "question. Ignore differences in wording, length, formatting, and citation "
    "style.\n"
    '- "correct": states the substance of the reference answer, with no false '
    "claims.\n"
    '- "partial": some correct substance, but incomplete, hedged to the point '
    "of being unhelpful, or mixed with an inaccuracy.\n"
    '- "wrong": fails to answer, contradicts the reference, or invents things.\n'
    "An answer that honestly says it could not find the information is "
    '"wrong" (it did not answer), never "partial".'
)

_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "partial", "wrong"]},
        "reason": {"type": "string", "description": "One sentence justifying the verdict."},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


@dataclass
class RetrievalScore:
    """How much of the expected code the system actually surfaced."""

    expected: list[Location]
    retrieved: list[Location]
    #: See EvalQuestion.accept_any — one answer with several valid evidence
    #: locations, rather than an enumeration.
    accept_any: bool = False

    @property
    def matched(self) -> int:
        """Expected locations that at least one retrieved location covers."""
        return sum(
            1
            for want in self.expected
            if any(candidate.covers(want) for candidate in self.retrieved)
        )

    @property
    def recall(self) -> float:
        """Of the locations that answer the question, how many were retrieved."""
        if not self.expected:
            return 0.0
        if self.accept_any:
            return 1.0 if self.matched else 0.0
        return self.matched / len(self.expected)

    @property
    def precision(self) -> float:
        """Of what was retrieved, how much was relevant.

        Retrieval breadth is not free: it fills the model's context with code
        that does not answer the question. Low precision with high recall means
        the answer came from wading through noise.
        """
        if not self.retrieved:
            return 0.0
        useful = sum(
            1
            for candidate in self.retrieved
            if any(candidate.covers(want) for want in self.expected)
        )
        return useful / len(self.retrieved)

    @property
    def hit(self) -> bool:
        """Did retrieval surface at least one location that answers the question."""
        return self.matched > 0


def score_retrieval(
    retrieved: list[Location], expected: list[Location], accept_any: bool = False
) -> RetrievalScore:
    return RetrievalScore(
        expected=list(expected), retrieved=list(retrieved), accept_any=accept_any
    )


@dataclass
class AnswerGrade:
    verdict: Verdict
    reason: str
    missing_mentions: list[str]

    @property
    def is_correct(self) -> bool:
        return self.verdict == "correct"


def check_mentions(answer: str, must_mention: list[str]) -> list[str]:
    """Required identifiers absent from the answer, matched case-insensitively."""
    lowered = answer.lower()
    return [item for item in must_mention if item.lower() not in lowered]


def grade_answer(
    provider: LLMProvider,
    question: str,
    reference_answer: str,
    actual_answer: str,
    must_mention: list[str],
    usage: Usage | None = None,
) -> AnswerGrade:
    """Grade one answer. The same grader and prompt are used for every system."""
    missing = check_mentions(actual_answer, must_mention)

    if not actual_answer.strip():
        return AnswerGrade("wrong", "Empty answer.", missing)

    prompt = (
        f"Question:\n{question}\n\n"
        f"Reference answer:\n{reference_answer}\n\n"
        f"Answer to grade:\n{actual_answer}\n\n"
        f"Grade the answer."
    )
    try:
        payload, response = provider.generate_json(
            prompt,
            _GRADE_SCHEMA,
            system=_GRADER_SYSTEM,
            max_tokens=GRADER_MAX_TOKENS,
            effort="low",
        )
    except Exception as exc:  # noqa: BLE001
        # Any provider failure — a rate limit, a malformed response, a network
        # blip — must degrade this one grade, never end a run that may already
        # represent an hour of paid calls.
        return AnswerGrade("wrong", f"Grader failed: {exc}", missing)

    if usage is not None:
        usage.record("grade", response)
    return AnswerGrade(payload["verdict"], payload.get("reason", ""), missing)
