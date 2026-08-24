"""Turn a run into tables meant to be read, and pasted into the README."""

from __future__ import annotations

from dataclasses import dataclass

from evaluation.dataset import CATEGORIES
from evaluation.runner import EvalRun, QuestionResult

LABELS = {
    "baseline": "naive RAG",
    "single_hop": "graph (1 query)",
    "multi_hop": "graph agent",
}


@dataclass
class Aggregate:
    system: str
    total: int
    correct: int
    partial: int
    wrong: int
    recall: float
    precision: float
    tokens: int
    seconds: float

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def aggregate(results: list[QuestionResult], system: str) -> Aggregate:
    subset = [item for item in results if item.system == system]
    count = len(subset)
    if not count:
        return Aggregate(system, 0, 0, 0, 0, 0.0, 0.0, 0, 0.0)

    return Aggregate(
        system=system,
        total=count,
        correct=sum(1 for item in subset if item.verdict == "correct"),
        partial=sum(1 for item in subset if item.verdict == "partial"),
        wrong=sum(1 for item in subset if item.verdict == "wrong"),
        recall=sum(item.retrieval_recall for item in subset) / count,
        precision=sum(item.retrieval_precision for item in subset) / count,
        tokens=sum(item.tokens for item in subset),
        seconds=sum(item.seconds for item in subset),
    )


def _row(values: list[str]) -> str:
    return "| " + " | ".join(values) + " |"


def overall_table(run: EvalRun, systems: tuple[str, ...]) -> str:
    lines = [
        _row(["system", "correct", "partial", "wrong", "accuracy", "recall", "precision", "tokens"]),
        _row(["---"] * 8),
    ]
    for system in systems:
        stats = aggregate(run.results, system)
        if not stats.total:
            continue
        lines.append(
            _row(
                [
                    LABELS.get(system, system),
                    f"{stats.correct}/{stats.total}",
                    str(stats.partial),
                    str(stats.wrong),
                    f"{stats.accuracy:.0%}",
                    f"{stats.recall:.2f}",
                    f"{stats.precision:.2f}",
                    f"{stats.tokens:,}",
                ]
            )
        )
    return "\n".join(lines)


def by_category_table(run: EvalRun, systems: tuple[str, ...]) -> str:
    """Where each approach wins is more informative than the headline number."""
    header = ["category", *[LABELS.get(system, system) for system in systems]]
    lines = [_row(header), _row(["---"] * len(header))]

    for category in CATEGORIES:
        in_category = [item for item in run.results if item.category == category]
        if not in_category:
            continue
        cells = [category.replace("_", "-")]
        for system in systems:
            subset = [item for item in in_category if item.system == system]
            if not subset:
                cells.append("-")
                continue
            correct = sum(1 for item in subset if item.verdict == "correct")
            cells.append(f"{correct}/{len(subset)}")
        lines.append(_row(cells))
    return "\n".join(lines)


def disagreements(run: EvalRun, systems: tuple[str, ...]) -> str:
    """Questions where the systems did not agree — the interesting cases."""
    by_question: dict[str, dict[str, QuestionResult]] = {}
    for item in run.results:
        by_question.setdefault(item.question_id, {})[item.system] = item

    lines = []
    for question_id, per_system in sorted(by_question.items()):
        verdicts = {system: per_system[system].verdict for system in systems if system in per_system}
        if len(set(verdicts.values())) <= 1:
            continue
        summary = ", ".join(f"{LABELS.get(s, s)}={v}" for s, v in verdicts.items())
        lines.append(f"  {question_id}: {summary}")
    return "\n".join(lines) if lines else "  (none — every system agreed on every question)"


def render(run: EvalRun, systems: tuple[str, ...]) -> str:
    parts = [
        f"model: {run.model}",
        "",
        "OVERALL",
        overall_table(run, systems),
        "",
        "ANSWER ACCURACY BY QUESTION TYPE",
        by_category_table(run, systems),
        "",
        "WHERE THE SYSTEMS DISAGREED",
        disagreements(run, systems),
    ]
    return "\n".join(parts)
