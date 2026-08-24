"""Run every system over the question set and score the results.

Three systems are compared, all on the same questions, the same LLM, the same
embedding model and the same grader:

* ``baseline``    — naive chunk-and-embed RAG.
* ``single_hop``  — one generated Cypher query (Phase 2).
* ``multi_hop``   — the agentic loop (Phase 3).

Holding the model constant is the point. If the systems ran on different models
a score gap could reflect model capability rather than retrieval strategy, and
the comparison would answer a question nobody asked.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

from agent.agent_loop import MultiHopAgent
from agent.provider import LLMProvider, Usage
from agent.query_agent import QueryAgent
from evaluation.baseline import NaiveRAG
from evaluation.dataset import EvalQuestion
from evaluation.metrics import AnswerGrade, grade_answer, score_retrieval
from retrieval.locations import Location
from retrieval.tools import RetrievalTools

logger = logging.getLogger(__name__)

SYSTEMS = ("baseline", "single_hop", "multi_hop")


@dataclass
class QuestionResult:
    question_id: str
    system: str
    category: str
    answer: str
    verdict: str
    grade_reason: str
    missing_mentions: list[str]
    retrieval_recall: float
    retrieval_precision: float
    retrieval_hit: bool
    retrieved: list[str]
    expected: list[str]
    tokens: int
    llm_calls: int
    seconds: float
    error: str = ""

    @property
    def correct(self) -> bool:
        return self.verdict == "correct"


@dataclass
class EvalRun:
    model: str
    results: list[QuestionResult] = field(default_factory=list)

    def for_system(self, system: str) -> list[QuestionResult]:
        return [item for item in self.results if item.system == system]

    def save(self, path: Path | str) -> None:
        Path(path).write_text(
            json.dumps(
                {"model": self.model, "results": [asdict(item) for item in self.results]},
                indent=2,
            ),
            encoding="utf-8",
        )


#: A free tier's tokens-per-minute cap is hit as a 429 mid-run. Waiting it out
#: is almost always right: the alternative is discarding work already paid for.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF = 20.0


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "429" in text


def with_rate_limit_retry(call, retries: int = RATE_LIMIT_RETRIES):
    """Run ``call``, waiting and retrying when the provider reports a rate limit."""
    for attempt in range(retries + 1):
        try:
            return call()
        except Exception as exc:
            if not _is_rate_limit(exc) or attempt == retries:
                raise
            delay = RATE_LIMIT_BACKOFF * (attempt + 1)
            logger.info("rate limited, waiting %.0fs before retrying", delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


def _answer_with_system(
    system: str,
    question: EvalQuestion,
    *,
    baseline: NaiveRAG,
    single: QueryAgent,
    multi: MultiHopAgent,
) -> tuple[str, list[Location], Usage]:
    if system == "baseline":
        result = baseline.answer(question.repo_id, question.question)
        return result.answer, result.locations, result.usage
    if system == "single_hop":
        result = single.answer(question.question)
        return result.answer, result.locations, result.usage
    result = multi.answer(question.question)
    return result.answer, result.locations, result.usage


def run_eval(
    questions: list[EvalQuestion],
    provider: LLMProvider,
    client,
    repo_path: Path,
    *,
    systems: tuple[str, ...] = SYSTEMS,
    grader: LLMProvider | None = None,
    pause_seconds: float = 0.0,
    model_label: str = "",
    save_to: Path | str | None = None,
) -> EvalRun:
    """Answer and grade every question with every system.

    ``pause_seconds`` paces calls for a provider with a tokens-per-minute cap;
    it is unnecessary on a paid provider and costs only wall-clock time.
    """
    grader = grader or provider
    repo_id = questions[0].repo_id

    baseline = NaiveRAG(provider, repo_path)
    single = QueryAgent(provider, client, repo_id)
    multi = MultiHopAgent(provider, RetrievalTools(client, repo_id, repo_path=repo_path))

    run = EvalRun(model=model_label or getattr(provider, "model", "unknown"))

    for index, question in enumerate(questions, start=1):
        for system in systems:
            started = time.perf_counter()
            usage = Usage()
            error = ""
            answer = ""
            retrieved: list[Location] = []

            try:
                answer, retrieved, usage = with_rate_limit_retry(
                    partial(
                        _answer_with_system,
                        system,
                        question,
                        baseline=baseline,
                        single=single,
                        multi=multi,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one failure must not end the run
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("%s failed on %s: %s", system, question.id, error)

            elapsed = time.perf_counter() - started
            retrieval = score_retrieval(retrieved, question.expected_locations)

            if error:
                grade = AnswerGrade("wrong", error, list(question.must_mention))
            else:
                grade = with_rate_limit_retry(
                    partial(
                        grade_answer,
                        grader,
                        question.question,
                        question.reference_answer,
                        answer,
                        question.must_mention,
                        usage=usage,
                    )
                )

            run.results.append(
                QuestionResult(
                    question_id=question.id,
                    system=system,
                    category=question.category,
                    answer=answer,
                    verdict=grade.verdict,
                    grade_reason=grade.reason,
                    missing_mentions=grade.missing_mentions,
                    retrieval_recall=round(retrieval.recall, 3),
                    retrieval_precision=round(retrieval.precision, 3),
                    retrieval_hit=retrieval.hit,
                    retrieved=[str(item) for item in retrieved],
                    expected=[str(item) for item in question.expected_locations],
                    tokens=usage.total_tokens,
                    llm_calls=usage.calls,
                    seconds=round(elapsed, 1),
                    error=error,
                )
            )
            logger.info(
                "[%d/%d] %s %s -> %s (recall %.2f, %d tok)",
                index,
                len(questions),
                question.id,
                system,
                grade.verdict,
                retrieval.recall,
                usage.total_tokens,
            )
            # Written after every result, not at the end: a run represents real
            # spend, and an interruption should cost the remaining questions,
            # not the completed ones.
            if save_to:
                run.save(save_to)
            if pause_seconds:
                time.sleep(pause_seconds)

    return run
