from __future__ import annotations

from conftest import ChunkedProvider, FakeNeo4j, FakeProvider

from agent.agent_loop import TOOL_SPECS, Hop, MultiHopAgent
from agent.provider import LLMResponse, ToolCall
from retrieval.tools import RetrievalTools, ToolResult

VALID_CYPHER = "MATCH (f:Function {repo_id: $repo_id}) RETURN f.name AS name LIMIT 25"


class FakeTools(RetrievalTools):
    """Records which tools ran, without touching Neo4j or Chroma."""

    def __init__(self, results: dict[str, ToolResult] | None = None):
        self.results = results or {}
        self.calls: list[tuple[str, str]] = []

    def _record(self, name: str, argument: str) -> ToolResult:
        self.calls.append((name, argument))
        return self.results.get(name, ToolResult(True, f"{name} ran"))

    def graph_query(self, cypher):
        return self._record("graph_query", cypher)

    def semantic_search(self, query, limit=6):
        return self._record("semantic_search", query)

    def read_code(self, qualified_name):
        return self._record("read_code", qualified_name)

    def grep(self, pattern):
        return self._record("grep", pattern)


def _tool_turn(name: str, arguments: dict, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        text="",
        model="fake",
        input_tokens=30,
        output_tokens=10,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
    )


def _text_turn(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="fake", input_tokens=30, output_tokens=10)


def test_tool_specs_cover_every_dispatchable_tool():
    names = {spec["function"]["name"] for spec in TOOL_SPECS}
    assert names == {"graph_query", "semantic_search", "read_code", "grep"}


def test_answers_without_tools_when_none_are_needed():
    provider = FakeProvider(turns=[_text_turn("Direct answer.")])
    result = MultiHopAgent(provider, FakeTools()).answer("hello")

    assert result.answer == "Direct answer."
    assert result.hops == []


def test_runs_a_tool_then_answers():
    provider = FakeProvider(
        turns=[_tool_turn("graph_query", {"cypher": VALID_CYPHER}), _text_turn("Found it.")]
    )
    tools = FakeTools()
    result = MultiHopAgent(provider, tools).answer("what calls send?")

    assert [name for name, _ in tools.calls] == ["graph_query"]
    assert result.answer == "Found it."
    assert len(result.hops) == 1
    assert result.hops[0].ok


def test_chains_multiple_tools_across_hops():
    provider = FakeProvider(
        turns=[
            _tool_turn("semantic_search", {"query": "retry handling"}, "a"),
            _tool_turn("graph_query", {"cypher": VALID_CYPHER}, "b"),
            _tool_turn("read_code", {"qualified_name": "Session.send"}, "c"),
            _text_turn("Retries are handled in HTTPAdapter.send."),
        ]
    )
    tools = FakeTools()
    result = MultiHopAgent(provider, tools).answer("how does retry work?")

    assert [name for name, _ in tools.calls] == ["semantic_search", "graph_query", "read_code"]
    assert len(result.hops) == 3
    assert "HTTPAdapter" in result.answer


def test_tool_results_are_fed_back_to_the_model():
    provider = FakeProvider(
        turns=[_tool_turn("grep", {"pattern": "retry"}), _text_turn("done")]
    )
    tools = FakeTools({"grep": ToolResult(True, "adapters.py:12: max_retries")})
    MultiHopAgent(provider, tools).answer("where is retry configured?")

    # The second conversation must contain the tool's output as a tool message.
    second_turn = provider.conversations[1]
    tool_messages = [m for m in second_turn if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "max_retries" in tool_messages[0].content


def test_a_failing_tool_does_not_end_the_investigation():
    provider = FakeProvider(
        turns=[
            _tool_turn("graph_query", {"cypher": "DELETE everything"}, "a"),
            _tool_turn("semantic_search", {"query": "fallback"}, "b"),
            _text_turn("Recovered."),
        ]
    )
    tools = FakeTools({"graph_query": ToolResult(False, "Query rejected: write clause")})
    result = MultiHopAgent(provider, tools).answer("q")

    assert result.hops[0].ok is False
    assert result.answer == "Recovered."


def test_malformed_tool_arguments_are_reported_not_raised():
    provider = FakeProvider(
        turns=[
            _tool_turn("graph_query", {"__malformed__": "{bad json"}),
            _text_turn("handled"),
        ]
    )
    result = MultiHopAgent(provider, FakeTools()).answer("q")

    assert result.hops[0].ok is False
    assert "not valid JSON" in result.hops[0].result


def test_an_unknown_tool_name_is_reported_not_raised():
    provider = FakeProvider(turns=[_tool_turn("nonexistent", {}), _text_turn("ok")])
    result = MultiHopAgent(provider, FakeTools()).answer("q")

    assert result.hops[0].ok is False
    assert "No tool named" in result.hops[0].result


def test_hop_limit_forces_a_final_answer():
    # Three tool turns exhaust max_hops; the fourth call is the forced answer.
    turns = [_tool_turn("grep", {"pattern": "x"}, f"c{i}") for i in range(3)]
    turns.append(_text_turn("Partial answer from what I found."))
    provider = FakeProvider(turns=turns)

    result = MultiHopAgent(provider, FakeTools(), max_hops=3).answer("endless")

    assert result.hit_hop_limit
    assert len(result.hops) == 3
    assert result.answer == "Partial answer from what I found."


def test_hop_limit_with_an_empty_final_response_still_says_something():
    turns = [_tool_turn("grep", {"pattern": "x"}, f"c{i}") for i in range(2)]
    turns.append(_text_turn(""))
    provider = FakeProvider(turns=turns)

    result = MultiHopAgent(provider, FakeTools(), max_hops=2).answer("endless")

    assert result.hit_hop_limit
    assert "could not answer" in result.answer
    assert "grep" in result.answer


def test_usage_accumulates_across_hops():
    provider = FakeProvider(
        turns=[_tool_turn("graph_query", {"cypher": VALID_CYPHER}), _text_turn("done")]
    )
    result = MultiHopAgent(provider, FakeTools()).answer("q")

    assert result.usage.calls == 2
    assert result.usage.total_tokens == 80


def test_hop_summary_is_readable():
    hop = Hop("graph_query", "MATCH (n) RETURN n", True, "1 row")
    assert hop.summary().startswith("graph_query(MATCH")
    assert Hop("grep", "x", False, "boom").summary().endswith("[failed]")


def test_the_schema_and_examples_are_in_the_system_prompt():
    provider = FakeProvider(turns=[_text_turn("hi")])
    MultiHopAgent(provider, FakeTools()).answer("q")

    system = provider.conversations[0][0]
    assert system.role == "system"
    assert "GRAPH SCHEMA" in system.content
    assert "$repo_id" in system.content


def test_answer_deltas_are_forwarded_in_order_and_match_the_final_answer():
    # The trailing space is part of the fixture: the concatenation must equal
    # the pre-tidy text exactly, gaps and all — chunk boundaries are arbitrary.
    provider = ChunkedProvider(
        turns=[_text_turn("Retries live in HTTPAdapter.send api.py:12.")],
        chunks=["Retries live ", "in HTTPAdapter.send", " api.py:12."],
    )
    received: list[str] = []
    result = MultiHopAgent(provider, FakeTools(), on_answer_delta=received.append).answer("q")

    assert received == ["Retries live ", "in HTTPAdapter.send", " api.py:12."]
    assert "".join(received) == "Retries live in HTTPAdapter.send api.py:12."
    assert result.answer == "Retries live in HTTPAdapter.send api.py:12."


def test_answer_deltas_still_flow_through_tool_hops_before_the_final_turn():
    provider = ChunkedProvider(
        turns=[
            _tool_turn("graph_query", {"cypher": VALID_CYPHER}),
            _text_turn("Found it."),
        ],
        chunks=["Found ", "it."],
    )
    received: list[str] = []
    result = MultiHopAgent(
        provider, FakeTools(), on_answer_delta=received.append
    ).answer("what calls send?")

    assert received == ["Found ", "it."]
    assert result.answer == "Found it."
    assert len(result.hops) == 1


def test_graph_query_tool_enforces_the_cypher_guard():
    # The real tool, not the fake: a write must be rejected before execution.
    tools = RetrievalTools(FakeNeo4j(), "owner/repo", vector_store=object())
    result = tools.graph_query("MATCH (n {repo_id: $repo_id}) DETACH DELETE n")

    assert not result.ok
    assert "rejected" in result.text.lower()
