"""OpenRouter implementation of :class:`~agent.provider.LLMProvider`.

OpenRouter has no first-party SDK worth adding as a dependency — it is a plain
REST API, OpenAI-compatible in shape, so this talks to it directly over
``httpx``.

The only provider implementation — see docs/ARCHITECTURE.md section 2.4a for
what else was tried and rejected. Two things this module deliberately does
NOT do, both learned by testing rather than assumed:

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
from collections.abc import Iterator

import httpx

from agent.provider import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamComplete,
    TextDelta,
    ToolCall,
)

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
    ) -> LLMResponse:
        return self._complete(prompt, system, max_tokens)

    def generate_json(
        self,
        prompt: str,
        json_schema: dict,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
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

    def converse_stream(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 2048,
    ) -> Iterator[TextDelta | StreamComplete]:
        payload = {
            "model": self.model,
            "messages": [_to_wire(message) for message in messages],
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "stream": True,
        }

        text_parts: list[str] = []
        # Tool-call fragments arrive split across chunks, keyed by index: the
        # id and function name each show up once, `arguments` is concatenated
        # across every chunk that carries a piece of it.
        pending_calls: dict[int, dict] = {}
        model_name = self.model
        usage: dict | None = None

        with self._client.stream("POST", CHAT_COMPLETIONS_PATH, json=payload) as response:
            if response.status_code != 200:
                response.read()
                raise RuntimeError(
                    f"OpenRouter request failed ({response.status_code}): "
                    f"{response.text[:500]}"
                )
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[len("data: ") :]
                if raw == "[DONE]":
                    break
                chunk = json.loads(raw)

                if chunk.get("usage"):
                    usage = chunk["usage"]
                if chunk.get("model"):
                    model_name = chunk["model"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    yield TextDelta(content)

                for tc in delta.get("tool_calls") or []:
                    index = tc.get("index", 0)
                    slot = pending_calls.setdefault(
                        index, {"id": None, "name": None, "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    function = tc.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]

        calls = [
            ToolCall(id=slot["id"], name=slot["name"], arguments=_parse_args_str(slot["arguments"]))
            for _, slot in sorted(pending_calls.items())
        ]
        usage = usage or {}
        details = usage.get("completion_tokens_details") or {}
        yield StreamComplete(
            LLMResponse(
                text="".join(text_parts),
                model=model_name,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                reasoning_tokens=details.get("reasoning_tokens", 0) or 0,
                tool_calls=calls,
            )
        )


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
    return _parse_args_str(call.get("function", {}).get("arguments"))


def _parse_args_str(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
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
