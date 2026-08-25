from __future__ import annotations

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
