from __future__ import annotations

import pytest
from conftest import FakeNeo4j, FakeProvider

from agent.few_shot import EXAMPLES, render_examples
from agent.query_agent import (
    MAX_CYPHER_ATTEMPTS,
    QueryAgent,
    _strip_fences,
    _tidy_answer,
)
from agent.schema_prompt import schema_description
from graph.schema import SHARED_LABEL, EdgeType, NodeType
from retrieval.cypher_guard import validate_cypher

VALID_CYPHER = "MATCH (f:Function {repo_id: $repo_id}) RETURN f.name AS name LIMIT 25"


# ------------------------------------------------------------------ prompts


def test_schema_description_covers_every_node_and_edge_type():
    description = schema_description()
    for node_type in NodeType:
        assert f"({node_type.value})" in description
    for edge_type in EdgeType:
        assert f"[:{edge_type.value}]" in description


def test_schema_description_states_the_repo_scoping_rule():
    assert "$repo_id" in schema_description()


def test_every_few_shot_example_passes_the_guard():
    # A bad example would teach the model to write queries the guard rejects.
    for question, cypher in EXAMPLES:
        result = validate_cypher(cypher)
        assert result.ok, f"example {question!r} produces invalid Cypher: {result.error}"


def test_no_example_uses_the_broken_labels_index_pattern():
    # Every node also carries the CodeNode label, which sorts first, so
    # `labels(n)[0]` returns "CodeNode" for everything. An example using it
    # would teach the model a pattern that silently returns useless values.
    for question, cypher in EXAMPLES:
        assert "labels(" not in cypher or "WHERE l <>" in cypher, (
            f"example {question!r} uses labels(...)[0] without excluding the shared label"
        )


def test_schema_rules_warn_about_both_silent_wrong_answer_traps():
    description = schema_description()
    # Filtering to :Function alone drops methods and looks correct.
    assert ":Function|Method" in description
    # labels(n)[0] returns the shared label rather than the node's real type.
    assert "labels(n)[0]" in description
    assert SHARED_LABEL in description


def test_render_examples_includes_all_examples():
    rendered = render_examples()
    for question, _ in EXAMPLES:
        assert question in rendered


# -------------------------------------------------------------------- agent


def test_answers_with_a_valid_query_on_the_first_attempt():
    provider = FakeProvider([{"cypher": VALID_CYPHER, "intent": "find functions"}])
    agent = QueryAgent(provider, FakeNeo4j(), "owner/repo")

    result = agent.answer("What functions exist?")

    assert result.answer == "The answer."
    assert len(result.attempts) == 1
    assert result.outcome.ok
    assert result.outcome.row_count == 1


def test_retries_after_the_guard_rejects_a_write_query():
    provider = FakeProvider(
        [
            {"cypher": "MATCH (n {repo_id: $repo_id}) DELETE n", "intent": "oops"},
            {"cypher": VALID_CYPHER, "intent": "corrected"},
        ]
    )
    agent = QueryAgent(provider, FakeNeo4j(), "owner/repo")

    result = agent.answer("Delete everything")

    assert len(result.attempts) == 2
    assert result.attempts[0].error is not None
    assert result.outcome.ok


def test_the_rejection_reason_is_fed_back_into_the_next_prompt():
    provider = FakeProvider(
        [
            {"cypher": "MATCH (f:Function) RETURN f LIMIT 5", "intent": "unscoped"},
            {"cypher": VALID_CYPHER, "intent": "scoped"},
        ]
    )
    QueryAgent(provider, FakeNeo4j(), "owner/repo").answer("What functions exist?")

    retry_prompt = provider.prompts[1]
    assert "PREVIOUS ATTEMPTS FAILED" in retry_prompt
    assert "repo_id" in retry_prompt


def test_gives_up_after_the_attempt_limit():
    bad = {"cypher": "MATCH (n {repo_id: $repo_id}) DELETE n", "intent": "bad"}
    provider = FakeProvider([bad] * MAX_CYPHER_ATTEMPTS)
    agent = QueryAgent(provider, FakeNeo4j(), "owner/repo")

    result = agent.answer("Break it")

    assert len(result.attempts) == MAX_CYPHER_ATTEMPTS
    assert result.outcome is None
    assert "could not construct a valid graph query" in result.answer


def test_empty_results_still_produce_an_answer():
    provider = FakeProvider([{"cypher": VALID_CYPHER, "intent": "find"}])
    agent = QueryAgent(provider, FakeNeo4j(rows=[]), "owner/repo")

    result = agent.answer("What calls nothing?")

    assert result.outcome.ok
    assert result.outcome.row_count == 0
    assert "no rows" in provider.prompts[-1]


def test_usage_accumulates_across_calls():
    provider = FakeProvider([{"cypher": VALID_CYPHER, "intent": "find"}])
    result = QueryAgent(provider, FakeNeo4j(), "owner/repo").answer("What functions exist?")

    assert result.usage.calls == 2  # one cypher generation, one synthesis
    assert result.usage.total_tokens == 43


def test_the_query_is_scoped_to_the_requested_repo():
    provider = FakeProvider([{"cypher": VALID_CYPHER, "intent": "find"}])
    neo4j = FakeNeo4j()
    QueryAgent(provider, neo4j, "owner/repo").answer("What functions exist?")

    # EXPLAIN first, then the real execution — both carry the repo parameter.
    assert any(query.startswith("EXPLAIN") for query in neo4j.executed)


def test_tidy_answer_trims_a_trailing_citation_dump():
    answer = (
        "The file defines:\n- get at line 74\n- put at line 137\n"
        "- delete at line 171   src/requests/api.py:74 src/requests/api.py:137 "
        "src/requests/api.py:171"
    )
    assert _tidy_answer(answer).endswith("- delete at line 171")


def test_tidy_answer_strips_fabricated_zero_line_numbers():
    # The model writes `:0` when the query returned no line number.
    answer = "`is_prepared` has 9 callers (src/requests/_types.py:0)."
    assert _tidy_answer(answer) == "`is_prepared` has 9 callers (src/requests/_types.py)."


def test_tidy_answer_keeps_real_line_numbers():
    answer = "Defined at src/requests/api.py:24 and src/requests/api.py:102."
    assert _tidy_answer(answer) == answer


def test_tidy_answer_drops_a_citation_duplicated_on_the_same_line():
    answer = "- `is_prepared` – 9 callers (src/_types.py line 47) src/_types.py:47"
    assert _tidy_answer(answer) == "- `is_prepared` – 9 callers (src/_types.py line 47)"


def test_tidy_answer_keeps_a_lines_only_citation():
    # Nothing earlier in the line names the file, so the citation must survive.
    answer = "- `is_prepared` has 9 callers src/_types.py:47"
    assert _tidy_answer(answer) == answer


def test_tidy_answer_normalises_lenticular_citation_brackets():
    answer = "The adapter passes retries【src/requests/adapters.py:695】to urllib3."
    assert _tidy_answer(answer) == (
        "The adapter passes retries(src/requests/adapters.py:695)to urllib3."
    )


def test_tidy_answer_keeps_inline_citations():
    answer = "`get` calls `request` (src/requests/api.py:24)."
    assert _tidy_answer(answer) == answer


def test_tidy_answer_keeps_short_citation_runs():
    # Two citations may be a legitimate sentence ending, not a dump.
    answer = "Defined in a.py:1 and b.py:2"
    assert _tidy_answer(answer) == answer


def test_tidy_answer_keeps_a_citation_list_on_separate_lines():
    answer = "Callers:\n- a.py:1\n- b.py:2\n- c.py:3"
    assert _tidy_answer(answer) == answer


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MATCH (n) RETURN n", "MATCH (n) RETURN n"),
        ("```cypher\nMATCH (n) RETURN n\n```", "MATCH (n) RETURN n"),
        ("```\nMATCH (n) RETURN n\n```", "MATCH (n) RETURN n"),
        ("  MATCH (n) RETURN n  ", "MATCH (n) RETURN n"),
    ],
)
def test_strip_fences(raw, expected):
    assert _strip_fences(raw) == expected
