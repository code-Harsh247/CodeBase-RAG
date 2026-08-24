"""LLM provider interface.

The agent never imports a vendor SDK directly. The provider changes across
phases of this project (Groq for development, a paid fallback reserved for the
evaluation run), so the boundary earns its keep — see docs/ARCHITECTURE.md
section 2.4a.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

#: How much internal reasoning to spend. Providers map this to their own knob;
#: on Groq it is `reasoning_effort`, which directly drives token burn.
Effort = Literal["low", "medium", "high"]


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: LLMResponse) -> LLMResponse:
        """Accumulate token usage across the calls that answer one question."""
        return LLMResponse(
            text=other.text,
            model=other.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass
class Usage:
    """Running token total for one answered question."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    per_call: list[str] = field(default_factory=list)

    def record(self, label: str, response: LLMResponse) -> None:
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.reasoning_tokens += response.reasoning_tokens
        self.per_call.append(f"{label}: {response.input_tokens}in/{response.output_tokens}out")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(ABC):
    """Minimal surface the agent depends on."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        effort: Effort = "medium",
    ) -> LLMResponse:
        """Free-form text generation."""

    @abstractmethod
    def generate_json(
        self,
        prompt: str,
        json_schema: dict,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        effort: Effort = "medium",
    ) -> tuple[dict, LLMResponse]:
        """Generation constrained to ``json_schema``. Returns the parsed object."""


def get_provider(name: str | None = None, model: str | None = None) -> LLMProvider:
    """Build the configured provider. Defaults come from the environment."""
    name = (name or os.environ.get("LLM_PROVIDER") or "groq").lower()

    if name == "groq":
        from agent.groq_provider import GroqProvider

        return GroqProvider(model=model or os.environ.get("LLM_MODEL"))

    raise ValueError(
        f"Unknown LLM provider {name!r}. Set LLM_PROVIDER to a supported value (groq)."
    )
