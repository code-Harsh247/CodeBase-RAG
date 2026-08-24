"""OpenRouter implementation of :class:`~agent.provider.LLMProvider`.

OpenRouter has no first-party SDK worth adding as a dependency — it is a plain
REST API, OpenAI-compatible in shape, so this talks to it directly over
``httpx`` (already installed transitively via the Groq client, but declared
explicitly since this module imports it directly).

Used as the pinned model for the Phase 4 scored eval run, not for day-to-day
development — see docs/ARCHITECTURE.md section 2.4a for why. Two things this
module deliberately does NOT do, both learned by testing rather than assumed:

* It never routes through ``openrouter/free``. That router picks a model at
  random per request and, tested directly, landed on a content-safety
  classifier for an ordinary chat prompt — unusable for anything here.
* It never omits ``max_tokens``. OpenRouter defaults to a model's max context
  when it is unset, which silently fails with a 402 on a small account
  balance long before it fails for a real reason.
"""

from __future__ import annotations

import json
import os

import httpx

from agent.provider import Effort, LLMProvider, LLMResponse, Message, ToolCall

#: Qwen3 Coder is purpose-built for agentic tool use over a codebase — see
#: docs/ARCHITECTURE.md for the comparison against alternatives that were
#: tested and rejected (weaker local models, other free/cheap options).
DEFAULT_MODEL = "qwen/qwen3-coder"

BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"
REQUEST_TIMEOUT = 120


class OpenRouterProvider(LLMProvider):
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Copy .env.example to .env and add a "
                "key from https://openrouter.ai/settings/keys."
            )
        self.model = model or DEFAULT_MODEL
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )

    def _messages(self, prompt: str, system: str | None) -> list[dict]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _post(self, payload: dict) -> dict:
        response = self._client.post(
            CHAT_COMPLETIONS_PATH, json={"model": self.model, **payload}
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"OpenRouter request failed ({response.status_code}): {response.text[:500]}"
            )
        return response.json()

    def _complete(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        response_format: dict | None = None,
    ) -> LLMResponse:
        payload: dict = {
            "messages": self._messages(prompt, system),
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        data = self._post(payload)
        return _to_response(data)

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        effort: Effort = "medium",
    ) -> LLMResponse:
        # `effort` has no uniform meaning across OpenRouter's model catalog —
        # unlike Groq, where every request goes to one known model — so it is
        # accepted for interface parity and not forwarded.
        return self._complete(prompt, system, max_tokens)

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
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            },
        )
        if not response.text.strip():
            raise ValueError(
                f"Model returned no output within {max_tokens} max_tokens."
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
        data = self._post(
            {
                "messages": [_to_wire(message) for message in messages],
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": max_tokens,
            }
        )
        return _to_response(data)


def _to_response(data: dict) -> LLMResponse:
    message = data["choices"][0]["message"]
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}

    calls = [
        ToolCall(id=call["id"], name=call["function"]["name"], arguments=_parse_args(call))
        for call in (message.get("tool_calls") or [])
    ]
    return LLMResponse(
        text=message.get("content") or "",
        model=data.get("model", ""),
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        reasoning_tokens=details.get("reasoning_tokens", 0) or 0,
        tool_calls=calls,
    )


def _parse_args(call: dict) -> dict:
    """Tool arguments arrive as a JSON string and may be malformed."""
    raw = call.get("function", {}).get("arguments") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"__malformed__": raw}


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
