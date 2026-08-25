"""LLM provider interface.

The agent never imports a vendor SDK directly. OpenRouter is the only
implementation today — Groq was dropped once its free tier's 200k tokens/day
stopped covering a UI session — but the boundary still earns its keep: it is
what `FakeProvider` substitutes for in the tests, and what a second provider
would slot into. See docs/ARCHITECTURE.md section 2.4a for what was tried and
rejected along the way (Gemini's free tier, OpenRouter's free-model router,
two local Ollama models).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    """One turn of a tool-using conversation, in vendor-neutral form."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Set on a `tool` message to say which call it answers.
    tool_call_id: str | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)

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
class TextDelta:
    """One fragment of visible text as it arrives from a streamed turn."""

    text: str


@dataclass
class StreamComplete:
    """Terminates a stream. Carries the same `LLMResponse` a blocking call
    would have returned — callers that don't care about incremental delivery
    can ignore every `TextDelta` and just collect this."""

    response: LLMResponse


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
    ) -> tuple[dict, LLMResponse]:
        """Generation constrained to ``json_schema``. Returns the parsed object."""

    @abstractmethod
    def converse(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """One turn of a tool-using conversation.

        Returns either text (the agent is done) or tool calls to execute and
        feed back as ``tool`` messages.
        """

    @abstractmethod
    def converse_stream(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 2048,
    ) -> Iterator[TextDelta | StreamComplete]:
        """Streamed form of :meth:`converse`.

        Whether this turn ends in tool calls or a text answer is not known
        until it completes, so every turn streams the same way; a caller
        watching for narration should forward ``TextDelta`` as it arrives and
        not assume the turn will end in text until ``StreamComplete`` says so.
        """


def get_provider(name: str | None = None, model: str | None = None) -> LLMProvider:
    """Build the configured provider. Defaults come from the environment."""
    name = (name or os.environ.get("LLM_PROVIDER") or "openrouter").lower()

    if name == "openrouter":
        from agent.openrouter_provider import OpenRouterProvider

        return OpenRouterProvider(model=model or os.environ.get("OPENROUTER_MODEL"))

    raise ValueError(
        f"Unknown LLM provider {name!r}. Set LLM_PROVIDER to a supported value "
        f"(openrouter)."
    )
