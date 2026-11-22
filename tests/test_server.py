from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import server
from api.server import HistoryTurn, QueryRequest, _history_messages


def _request(**kwargs) -> QueryRequest:
    return QueryRequest(repo_id="owner/repo", question="what calls it?", **kwargs)


def test_a_first_question_carries_no_history():
    assert _history_messages(_request()) == []


def test_turns_become_messages_in_order():
    request = _request(
        history=[
            HistoryTurn(role="user", content="what does send do?"),
            HistoryTurn(role="assistant", content="It sends a request."),
        ]
    )
    messages = _history_messages(request)

    assert [m.role for m in messages] == ["user", "assistant"]
    assert [m.content for m in messages] == ["what does send do?", "It sends a request."]


def test_the_summary_leads_as_a_system_message():
    request = _request(
        history_summary="Discussed Session.send and its callers.",
        history=[HistoryTurn(role="user", content="and retries?")],
    )
    messages = _history_messages(request)

    assert messages[0].role == "system"
    assert "Discussed Session.send" in messages[0].content
    assert messages[1].content == "and retries?"


def test_a_summary_alone_is_enough():
    # Right after a fold-in there may be no loose turns left at all.
    messages = _history_messages(_request(history_summary="Everything so far."))

    assert len(messages) == 1
    assert messages[0].role == "system"


def test_oversized_history_is_trimmed_before_it_reaches_the_model():
    # The client bounds this too, but its fold-in may still be in flight when
    # the next question fires — this is the last checkpoint before the prompt.
    request = _request(
        history=[HistoryTurn(role="user", content=f"question {i} " * 200) for i in range(20)]
    )
    messages = _history_messages(request)

    assert len(messages) < 20, "an over-budget history must not pass through intact"


def test_trimming_keeps_the_most_recent_turns():
    request = _request(
        history=[
            HistoryTurn(role="user", content="ancient " * 400),
            HistoryTurn(role="assistant", content="old " * 400),
            HistoryTurn(role="user", content="what calls it?"),
        ]
    )
    messages = _history_messages(request)

    assert messages, "the newest turn should survive"
    assert messages[-1].content == "what calls it?"


# ----------------------------------------------------------------- hardening

@pytest.mark.parametrize("repo_id", ["../../etc", "owner/..", "a/b/c", "owner"])
def test_query_request_rejects_path_like_repo_ids(repo_id):
    with pytest.raises(ValidationError):
        QueryRequest(repo_id=repo_id, question="hi")


def test_query_request_rejects_unknown_modes():
    with pytest.raises(ValidationError):
        QueryRequest(repo_id="owner/repo", question="hi", mode="drop_everything")


def test_query_endpoint_returns_422_for_a_traversal_repo_id():
    response = TestClient(server.app).post(
        "/api/query", json={"repo_id": "../../etc", "question": "hi"}
    )
    assert response.status_code == 422


def test_ingest_endpoint_rejects_non_github_urls_before_streaming():
    response = TestClient(server.app).post(
        "/api/ingest", json={"url": "file:///etc/passwd"}
    )
    assert response.status_code == 400


def _events(stream) -> list[dict]:
    return [json.loads(chunk.removeprefix("data: ").strip()) for chunk in stream]


def test_stream_errors_never_carry_exception_text(monkeypatch):
    def broken_client():
        raise RuntimeError("password=hunter2 at /home/ubuntu/apps/secret")

    monkeypatch.setattr(server, "_client", broken_client)
    events = _events(server._stream_answer(_request()))

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "internal"
    assert "hunter2" not in json.dumps(events)
    assert "/home" not in json.dumps(events)


def test_ingest_limit_errors_keep_their_code(monkeypatch):
    from ingestion.limits import IngestLimitError

    def too_big(*args, **kwargs):
        raise IngestLimitError("repo_too_large", "The repository has too many files.")

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(server, "_client", _Client)
    monkeypatch.setattr(server, "ingest", too_big)
    request = server.IngestRequest(url="https://github.com/owner/repo")
    events = _events(server._stream_ingest(request, "owner", "repo"))

    assert events[-1] == {
        "type": "error",
        "code": "repo_too_large",
        "message": "The repository has too many files.",
    }


def test_ingest_never_indexes_a_local_path(monkeypatch):
    seen = {}

    def fake_ingest(source, client, **kwargs):
        seen.update(kwargs, source=source)
        raise RuntimeError("stop")

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(server, "_client", _Client)
    monkeypatch.setattr(server, "ingest", fake_ingest)
    request = server.IngestRequest(url="https://github.com/owner/repo")
    list(server._stream_ingest(request, "owner", "repo"))

    assert seen["allow_local"] is False
    assert seen["source"] == "https://github.com/owner/repo"


def test_delete_rejects_traversal():
    # The HTTP client normalises "owner/.." away before it is sent, so call the
    # handler directly with the values a hand-crafted request could carry.
    from fastapi import HTTPException

    for owner, name in [("owner", ".."), ("..", "etc"), ("owner", "."), ("-x", "y")]:
        with pytest.raises(HTTPException) as caught:
            server.delete_repo(owner, name)
        assert caught.value.status_code == 400
