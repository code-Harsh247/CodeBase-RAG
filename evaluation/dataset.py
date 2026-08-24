"""Ground-truth questions for the evaluation.

Questions are written by reading the repository directly and confirming answers
against the source — never by running either system and recording what it said.
A ground truth derived from a system's own output measures self-consistency,
not correctness.

Each question carries the source locations that genuinely answer it, which is
what retrieval is scored against, and a short reference answer plus the facts
that must appear, which is what the answer grader is given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from retrieval.locations import Location

#: Structural: one relationship lookup ("what calls X").
#: Multi-hop: needs a name discovered before the real question can be asked.
#: Conceptual: phrased as behaviour, with no identifier given.
Category = Literal["structural", "multi_hop", "conceptual"]

CATEGORIES: tuple[Category, ...] = ("structural", "multi_hop", "conceptual")


@dataclass
class EvalQuestion:
    id: str
    repo_id: str
    question: str
    category: Category
    #: Locations that genuinely answer the question, for retrieval scoring.
    expected_locations: list[Location] = field(default_factory=list)
    #: A short correct answer, for the grader to compare against.
    reference_answer: str = ""
    #: Identifiers a correct answer has to name. Kept small and unambiguous.
    must_mention: list[str] = field(default_factory=list)
    notes: str = ""


def _parse_question(raw: dict, default_repo: str) -> EvalQuestion:
    category = raw.get("category", "structural")
    if category not in CATEGORIES:
        raise ValueError(f"{raw.get('id')}: unknown category {category!r}")

    return EvalQuestion(
        id=str(raw["id"]),
        repo_id=raw.get("repo_id", default_repo),
        question=raw["question"],
        category=category,
        expected_locations=[Location.parse(item) for item in raw.get("locations", [])],
        reference_answer=raw.get("answer", ""),
        must_mention=list(raw.get("must_mention", [])),
        notes=raw.get("notes", ""),
    )


def load_questions(path: Path | str) -> list[EvalQuestion]:
    """Load a question set from JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    default_repo = data.get("repo_id", "")
    questions = [_parse_question(raw, default_repo) for raw in data["questions"]]

    seen: set[str] = set()
    for question in questions:
        if question.id in seen:
            raise ValueError(f"Duplicate question id: {question.id}")
        seen.add(question.id)
    return questions


def summarise(questions: list[EvalQuestion]) -> str:
    counts = {category: 0 for category in CATEGORIES}
    for question in questions:
        counts[question.category] += 1
    parts = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
    return f"{len(questions)} questions ({parts})"
