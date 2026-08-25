"""Single-shot question answering: question -> Cypher -> results -> cited answer.

Phase 2 deliberately uses one graph query per question. Multi-hop tool use and
the vector/grep fallbacks arrive in Phase 3; keeping this pass simple makes the
comparison between the two measurable.

Invalid Cypher is not a dead end: the validation or database error is fed back
to the model for a bounded number of retries, which matters when the generating
model is a small free-tier one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from agent.few_shot import render_examples
from agent.provider import LLMProvider, Usage
from agent.schema_prompt import schema_description
from retrieval.graph_query import QueryOutcome, render_rows, run_query
from retrieval.locations import Location, dedupe, locations_from_rows

logger = logging.getLogger(__name__)

MAX_CYPHER_ATTEMPTS = 3

#: Room for the model to reason before emitting output — GPT-OSS spends part of
#: max_tokens on reasoning, and too small a budget yields an empty response.
CYPHER_MAX_TOKENS = 1200
ANSWER_MAX_TOKENS = 1500

_CYPHER_SCHEMA = {
    "type": "object",
    "properties": {
        "cypher": {"type": "string", "description": "The read-only Cypher query."},
        "intent": {
            "type": "string",
            "description": "One sentence on what the query retrieves.",
        },
    },
    "required": ["cypher", "intent"],
    "additionalProperties": False,
}

_CYPHER_SYSTEM = (
    "You translate questions about a Python codebase into read-only Cypher "
    "queries against a Neo4j knowledge graph. Return only the query — no "
    "explanation, no markdown fences."
)

_ANSWER_SYSTEM = (
    "You answer questions about a Python codebase using query results from a "
    "code knowledge graph.\n"
    "- Ground every claim in the rows provided. Never invent names, files, or "
    "line numbers.\n"
    "- Cite sources as plain `path/to/file.py:42`, inline beside the claim and "
    "once each. No brackets, footnotes, or reference markers, and never append "
    "a trailing list of citations.\n"
    "- Only cite a line number that appears verbatim in the rows. If a row has "
    "no line number, cite the file path alone. Never write `:0` and never "
    "guess a line — a fabricated location is worse than no location.\n"
    "- If the rows are empty, say the graph contains no match and suggest what "
    "the user might ask instead. Do not guess an answer.\n"
    "- This tool answers read-only questions about code structure. If the "
    "question asks to modify the database or is not about the codebase, say so "
    "briefly. Never supply write queries or database administration advice.\n"
    "- Be concise. Lead with the answer, not a description of the query."
)


@dataclass
class Attempt:
    cypher: str
    intent: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class QueryResult:
    question: str
    answer: str
    outcome: QueryOutcome | None = None
    attempts: list[Attempt] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    @property
    def cypher(self) -> str | None:
        return self.outcome.cypher if self.outcome else None

    @property
    def locations(self) -> list[Location]:
        """Source locations the executed query surfaced."""
        if self.outcome is None or not self.outcome.ok:
            return []
        return dedupe(locations_from_rows(self.outcome.rows))


class QueryAgent:
    """Answers one question with one graph query (plus retries on bad Cypher)."""

    def __init__(self, provider: LLMProvider, client, repo_id: str) -> None:
        self.provider = provider
        self.client = client
        self.repo_id = repo_id

    # ------------------------------------------------------------- prompting

    def _cypher_prompt(self, question: str, attempts: list[Attempt]) -> str:
        parts = [schema_description(), "", render_examples()]

        if attempts:
            parts.append("PREVIOUS ATTEMPTS FAILED — fix the problem described:")
            for attempt in attempts:
                parts.append(f"Query:\n{attempt.cypher}\nError: {attempt.error}")
            parts.append("")

        parts.append(f"Q: {question}")
        parts.append("Write the Cypher query that answers it.")
        return "\n".join(parts)

    def _answer_prompt(self, question: str, outcome: QueryOutcome) -> str:
        return (
            f"Question: {question}\n\n"
            f"Cypher executed:\n{outcome.cypher}\n\n"
            f"Results ({outcome.row_count} rows):\n{render_rows(outcome.rows)}\n\n"
            f"Answer the question from these results."
        )

    # ------------------------------------------------------------------- run

    def answer(self, question: str) -> QueryResult:
        result = QueryResult(question=question, answer="")

        outcome = self._generate_and_run(question, result)
        if outcome is None:
            result.answer = (
                "I could not construct a valid graph query for that question after "
                f"{MAX_CYPHER_ATTEMPTS} attempts. The last error was: "
                f"{result.attempts[-1].error}"
            )
            return result

        result.outcome = outcome
        response = self.provider.generate(
            self._answer_prompt(question, outcome),
            system=_ANSWER_SYSTEM,
            max_tokens=ANSWER_MAX_TOKENS,
        )
        result.usage.record("synthesis", response)
        result.answer = _tidy_answer(response.text)
        return result

    def _generate_and_run(self, question: str, result: QueryResult) -> QueryOutcome | None:
        for attempt_number in range(1, MAX_CYPHER_ATTEMPTS + 1):
            payload, response = self.provider.generate_json(
                self._cypher_prompt(question, result.attempts),
                _CYPHER_SCHEMA,
                system=_CYPHER_SYSTEM,
                max_tokens=CYPHER_MAX_TOKENS,
            )
            result.usage.record(f"cypher#{attempt_number}", response)

            cypher = _strip_fences(payload.get("cypher", ""))
            attempt = Attempt(cypher=cypher, intent=payload.get("intent", ""))

            outcome = run_query(self.client, cypher, self.repo_id)
            if outcome.ok:
                result.attempts.append(attempt)
                return outcome

            attempt.error = outcome.error
            result.attempts.append(attempt)
            logger.info("cypher attempt %d rejected: %s", attempt_number, outcome.error)

        return None


#: A run of bare `file:line` tokens trailing the answer. GPT-OSS appends these
#: regardless of prompt instructions, and they duplicate line numbers already
#: stated inline, so they are trimmed deterministically rather than by prompting.
_TRAILING_CITATIONS = re.compile(r"(?:[ \t]+[\w./\\-]+:\d+){3,}[ \t]*$", re.MULTILINE)


#: `file.py:0` is never a real location — it appears when the query returned no
#: line number and the model invented one rather than omitting the citation.
#: Prompting alone does not reliably stop this, and a fabricated source location
#: undermines the one guarantee this tool makes, so it is also stripped here.
_ZERO_LINE_CITATION = re.compile(r"([\w./\\-]+\.\w+):0\b")


#: A single citation tacked onto the end of a line, e.g.
#: "... (src/x.py line 47) src/x.py:47" or "... **src/x.py:47**(src/x.py:47)".
#: Allows the surrounding brackets and markdown emphasis the model tends to add.
#: Only removed when that same path already appears earlier in the line, so a
#: line whose only citation is at the end keeps it.
#: The opening bracket class deliberately excludes `*`: consuming it would eat
#: the closing emphasis of a preceding `**path**` and leave unbalanced markdown.
_TRAILING_ONE_CITATION = re.compile(
    r"[ \t]*[(\[]*\s*([\w./\\-]+\.\w+):(\d+)[)\]*.,]*[ \t]*$"
)


def _drop_duplicate_citation(line: str) -> str:
    """Drop a trailing citation that repeats one already made on the same line.

    Both the file *and* the line number have to match. Comparing paths alone
    would collapse "defined at api.py:24 and api.py:102" into a single
    citation, losing a real one.
    """
    match = _TRAILING_ONE_CITATION.search(line)
    if match is None:
        return line

    head = line[: match.start()]
    path, line_number = match.group(1), match.group(2)

    if f"{path}:{line_number}" in head:
        return head
    # The earlier mention may be prose ("(api.py line 47)") rather than a
    # citation, which is still the same location said twice.
    if path in head and re.search(rf"\b{re.escape(line_number)}\b", head):
        return head
    return line


#: GPT-OSS wraps citations in CJK lenticular brackets no matter what the prompt
#: says. Normalised to parentheses rather than fought over in the prompt.
_LENTICULAR = re.compile(r"【\s*([^】]*?)\s*】")


def _tidy_answer(answer: str) -> str:
    cleaned = _LENTICULAR.sub(r"(\1)", answer)
    cleaned = _TRAILING_CITATIONS.sub("", cleaned)
    cleaned = _ZERO_LINE_CITATION.sub(r"\1", cleaned)
    cleaned = "\n".join(_drop_duplicate_citation(line) for line in cleaned.splitlines())
    return cleaned.strip()


def _strip_fences(cypher: str) -> str:
    """Models wrap queries in markdown fences even when told not to."""
    text = cypher.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()
