"""Groq implementation of :class:`~agent.provider.LLMProvider`.

Defaults to GPT-OSS 120B on Groq's free tier. Two things about that model shape
this code: it is a reasoning model, so `max_tokens` must leave room for internal
reasoning before any visible output (a small budget returns an empty string, not
an error), and the free tier's 8,000 tokens/minute cap makes `reasoning_effort`
a cost control rather than a quality dial.
"""

from __future__ import annotations

import json
import os

from groq import Groq

from agent.provider import Effort, LLMProvider, LLMResponse, Message, ToolCall

DEFAULT_MODEL = "openai/gpt-oss-120b"


class GroqProvider(LLMProvider):
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Copy .env.example to .env and add a key "
                "from https://console.groq.com."
            )
        self.model = model or DEFAULT_MODEL
        self._client = Groq(api_key=key)

    def _messages(self, prompt: str, system: str | None) -> list[dict]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _complete(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        effort: Effort,
        response_format: dict | None = None,
    ) -> LLMResponse:
        kwargs = {
            "model": self.model,
            "messages": self._messages(prompt, system),
            "max_tokens": max_tokens,
            "reasoning_effort": effort,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        completion = self._client.chat.completions.create(**kwargs)
        usage = completion.usage
        details = getattr(usage, "completion_tokens_details", None)

        return LLMResponse(
            text=completion.choices[0].message.content or "",
            model=completion.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
        )

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        effort: Effort = "medium",
    ) -> LLMResponse:
        return self._complete(prompt, system, max_tokens, effort)

    def generate_json(
        self,
        prompt: str,
        json_schema: dict,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        effort: Effort = "medium",
    ) -> tuple[dict, LLMResponse]:
        response = self._complete(
            prompt,
            system,
            max_tokens,
            effort,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            },
        )
        if not response.text.strip():
            raise ValueError(
                f"Model returned no output; {response.reasoning_tokens} of "
                f"{max_tokens} max_tokens went to reasoning. Raise max_tokens "
                f"or lower effort."
            )
        return json.loads(response.text), response

    def converse(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 2048,
        effort: Effort = "medium",
    ) -> LLMResponse:
        completion = self._client.chat.completions.create(
            model=self.model,
            messages=[_to_wire(message) for message in messages],
            tools=tools,
            tool_choice="auto",
            max_tokens=max_tokens,
            reasoning_effort=effort,
        )
        choice = completion.choices[0]
        usage = completion.usage
        details = getattr(usage, "completion_tokens_details", None)

        calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=_parse_args(call))
            for call in (choice.message.tool_calls or [])
        ]
        return LLMResponse(
            text=choice.message.content or "",
            model=completion.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
            tool_calls=calls,
        )


def _parse_args(call) -> dict:
    """Tool arguments arrive as a JSON string and may be malformed."""
    try:
        return json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {"__malformed__": call.function.arguments}


def _to_wire(message: Message) -> dict:
    payload: dict = {"role": message.role, "content": message.content or ""}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload
