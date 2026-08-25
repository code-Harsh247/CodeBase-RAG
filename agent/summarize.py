"""Rolling conversation summary.

A fixed-size window over recent turns has a hard failure mode: once a thread
runs past the window, turn 1 is simply gone and a question that refers back to
it cannot be answered. Instead, older turns are folded into a running summary
that travels with every request, so the model keeps working knowledge of the
whole conversation at roughly constant cost.

Two pieces live here: building the fold-in prompt, and the last-resort trim
that bounds raw turns by real token count before they reach the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.provider import LLMProvider, LLMResponse
from agent.tokenizer import count_tokens

#: Ceiling on the un-summarized turns carried into a query. The frontend
#: normally keeps this well under budget by folding older turns into the
#: summary; this catches the case where it has not caught up yet — a summarize
#: call still in flight when the next question is asked.
HISTORY_TOKEN_BUDGET = 1500

#: The summary is re-sent on every hop, so it is kept to a few sentences.
SUMMARY_MAX_TOKENS = 300

_SYSTEM = """\
You maintain a running summary of a conversation about a codebase.

Fold the new exchanges into the existing summary and return the result. Keep \
it under 150 words. Preserve the concrete details a later question might refer \
back to — function, class and file names, and what was concluded about them. \
Drop pleasantries, restated questions, and anything already superseded.

Return only the summary text, with no preamble.
"""


@dataclass
class Turn:
    """One side of an exchange, in the same shape the API and agent both use."""

    role: str
    content: str


def _render(turns: list[Turn]) -> str:
    labels = {"user": "Q", "assistant": "A"}
    return "\n\n".join(f"{labels.get(turn.role, turn.role)}: {turn.content}" for turn in turns)


def build_prompt(prior_summary: str, turns: list[Turn]) -> str:
    if prior_summary:
        return (
            f"Existing summary:\n{prior_summary}\n\n"
            f"New exchanges to fold in:\n{_render(turns)}"
        )
    return f"Summarize these exchanges:\n{_render(turns)}"


def summarize(
    provider: LLMProvider, prior_summary: str, turns: list[Turn]
) -> tuple[str, LLMResponse]:
    """Fold `turns` into `prior_summary`. Returns the new summary and usage."""
    response = provider.generate(
        build_prompt(prior_summary, turns),
        system=_SYSTEM,
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    summary = response.text.strip()
    # An empty completion would silently erase an accumulated summary, which is
    # worse than carrying a slightly stale one forward.
    return (summary or prior_summary), response


def trim_to_budget(turns: list[Turn], budget: int = HISTORY_TOKEN_BUDGET) -> list[Turn]:
    """Drop whole turns from the oldest end until the rest fits `budget`.

    Counted with the real tokenizer for the model in use, not a character
    proxy: the ratio between the two swings several-fold between prose and
    dense code, and code is most of what these turns quote.
    """
    kept: list[Turn] = []
    total = 0
    for turn in reversed(turns):
        cost = count_tokens(turn.content)
        if total + cost > budget:
            break
        kept.append(turn)
        total += cost
    kept.reverse()
    return kept
