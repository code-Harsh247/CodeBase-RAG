from __future__ import annotations

import pytest

from retrieval.cypher_guard import strip_noise, validate_cypher

READ_QUERY = "MATCH (f:Function {repo_id: $repo_id}) RETURN f.name LIMIT 25"


def test_accepts_a_scoped_read_query():
    assert validate_cypher(READ_QUERY)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n {repo_id: $repo_id}) DELETE n",
        "MATCH (n {repo_id: $repo_id}) DETACH DELETE n",
        "CREATE (n:Function {repo_id: $repo_id})",
        "MERGE (n:Function {repo_id: $repo_id})",
        "MATCH (n {repo_id: $repo_id}) SET n.name = 'x'",
        "MATCH (n {repo_id: $repo_id}) REMOVE n.name",
        "DROP INDEX code_node_id",
        "MATCH (n {repo_id: $repo_id}) FOREACH (x IN [1] | SET n.a = 1)",
        "LOAD CSV FROM 'file:///etc/passwd' AS row RETURN row",
    ],
)
def test_rejects_write_operations(query):
    result = validate_cypher(query)
    assert not result
    assert result.error


def test_rejects_write_hidden_after_a_read():
    query = "MATCH (n {repo_id: $repo_id}) WITH n LIMIT 1 DETACH DELETE n RETURN 1"
    assert not validate_cypher(query)


def test_requires_repo_scoping():
    result = validate_cypher("MATCH (f:Function) RETURN f.name LIMIT 25")
    assert not result
    assert "repo_id" in result.error


def test_repo_scoping_can_be_waived():
    assert validate_cypher("MATCH (f:Function) RETURN f LIMIT 1", require_repo_scope=False)


def test_rejects_empty_query():
    assert not validate_cypher("")
    assert not validate_cypher("   ")


def test_write_keyword_inside_a_string_literal_is_not_a_write():
    # A function genuinely named `delete_user` must not trip the guard.
    query = (
        "MATCH (f:Function {repo_id: $repo_id}) WHERE f.name = 'delete_user' "
        "RETURN f.qualified_name LIMIT 25"
    )
    assert validate_cypher(query)


def test_write_keyword_inside_a_comment_is_not_a_write():
    query = "MATCH (f:Function {repo_id: $repo_id}) // could CREATE later\nRETURN f LIMIT 1"
    assert validate_cypher(query)


def test_import_node_label_is_not_mistaken_for_a_write_clause():
    # The `Import` label and `[:IMPORTS]` relationship must stay queryable.
    query = (
        "MATCH (m:Module {repo_id: $repo_id})-[:IMPORTS]->(i:Import) "
        "RETURN i.name AS name LIMIT 25"
    )
    assert validate_cypher(query)


def test_rejects_unsafe_procedures():
    query = "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {}) YIELD batches"
    assert not validate_cypher(query)


def test_strip_noise_blanks_literals_and_comments():
    stripped = strip_noise("MATCH (n) WHERE n.x = 'CREATE' /* MERGE */ RETURN n // DELETE")
    assert "CREATE" not in stripped
    assert "MERGE" not in stripped
    assert "DELETE" not in stripped
    assert "MATCH" in stripped
