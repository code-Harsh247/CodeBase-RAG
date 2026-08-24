from __future__ import annotations

from agent.openrouter_provider import _parse_args, _to_response, _to_wire
from agent.provider import Message, ToolCall


def test_to_wire_plain_message():
    assert _to_wire(Message(role="user", content="hi")) == {"role": "user", "content": "hi"}


def test_to_wire_assistant_message_with_tool_calls():
    message = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="c1", name="graph_query", arguments={"cypher": "MATCH (n) RETURN n"})],
    )
    wire = _to_wire(message)
    assert wire["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "graph_query", "arguments": '{"cypher": "MATCH (n) RETURN n"}'},
        }
    ]


def test_to_wire_tool_result_message_carries_call_id():
    message = Message(role="tool", content="5 rows", tool_call_id="c1")
    assert _to_wire(message) == {"role": "tool", "content": "5 rows", "tool_call_id": "c1"}


def test_to_wire_none_content_becomes_empty_string():
    assert _to_wire(Message(role="assistant", content=None))["content"] == ""


def test_parse_args_valid_json():
    call = {"function": {"arguments": '{"cypher": "MATCH (n) RETURN n"}'}}
    assert _parse_args(call) == {"cypher": "MATCH (n) RETURN n"}


def test_parse_args_malformed_json_is_reported_not_raised():
    call = {"function": {"arguments": "{not valid"}}
    result = _parse_args(call)
    assert "__malformed__" in result


def test_parse_args_missing_arguments_defaults_to_empty():
    assert _parse_args({"function": {}}) == {}


def test_to_response_extracts_text_and_usage():
    data = {
        "model": "qwen/qwen3-coder",
        "choices": [{"message": {"content": "hello", "role": "assistant"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = _to_response(data)
    assert response.text == "hello"
    assert response.model == "qwen/qwen3-coder"
    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert response.tool_calls == []


def test_to_response_extracts_tool_calls():
    data = {
        "model": "qwen/qwen3-coder",
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "graph_query", "arguments": '{"cypher": "x"}'},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = _to_response(data)
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "graph_query"
    assert response.tool_calls[0].arguments == {"cypher": "x"}


def test_to_response_handles_missing_content_and_usage():
    # A response with no text (pure tool call) and no usage block must not raise.
    data = {"model": "m", "choices": [{"message": {}}]}
    response = _to_response(data)
    assert response.text == ""
    assert response.input_tokens == 0
    assert response.output_tokens == 0
