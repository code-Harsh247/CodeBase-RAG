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


# ----------------------------------------------------------------- bypasses
# Each of these got past an earlier version of the guard.


@pytest.mark.parametrize(
    "query",
    [
        # A write hidden inside a string literal, executed by a procedure.
        "CALL apoc.cypher.doIt('MATCH (n) DETACH DELETE n', {}) YIELD value RETURN value",
        "MATCH (n {repo_id: $repo_id}) CALL apoc.cypher.runWrite('CREATE (x)', {}) RETURN n",
        # Backtick-quoted procedure names dodge a dotted-name pattern.
        "MATCH (n {repo_id: $repo_id}) CALL `apoc`.`cypher`.`doIt`('x', {}) YIELD value RETURN 1",
        "CALL db.labels() YIELD label RETURN label",
        # Subqueries need CALL too; nothing the agent asks for requires one.
        "CALL { MATCH (n {repo_id: $repo_id}) RETURN n } RETURN n",
    ],
)
def test_rejects_every_procedure_call(query):
    assert not validate_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        "SHOW DATABASES",
        "SHOW USERS",
        "USE system MATCH (n {repo_id: $repo_id}) RETURN n",
        "STOP DATABASE neo4j",
        "START DATABASE neo4j",
        "TERMINATE TRANSACTIONS 'neo4j-transaction-1'",
    ],
)
def test_rejects_admin_commands(query):
    assert not validate_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        # Stripping comments before strings let the `//` inside the literal eat
        # the rest of the line, hiding the write that follows it.
        "MATCH (n {repo_id: $repo_id}) WHERE n.name = '//' DETACH DELETE n",
        "MATCH (n {repo_id: $repo_id}) WHERE n.name = \"//\" DETACH DELETE n",
        "MATCH (n {repo_id: $repo_id}) WHERE n.name = '/*' DETACH DELETE n // */",
        # An escaped quote does not end the literal.
        r"MATCH (n {repo_id: $repo_id}) WHERE n.name = 'it\'s' DETACH DELETE n",
    ],
)
def test_comment_markers_inside_strings_do_not_hide_writes(query):
    assert not validate_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n {repo_id: $repo_id}) WHERE n.name = 'abc DETACH DELETE n",
        "MATCH (n {repo_id: $repo_id}) RETURN n /* DETACH DELETE n",
        "MATCH (n {repo_id: $repo_id}) RETURN `n DETACH DELETE n",
    ],
)
def test_unterminated_literals_fail_closed(query):
    result = validate_cypher(query)
    assert not result
    assert "unterminated" in result.error


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n {repo_id: $repo_id}), (m:Function) WHERE m.repo_id <> $repo_id RETURN m.name",
        "MATCH (n:Function {repo_id: $repo_id}) RETURN n.repo_id",
        "MATCH (n {repo_id: $repo_id}), (m) WHERE m.repo_id STARTS WITH 'a' RETURN m",
        "MATCH (n {repo_id: $repo_id}), (m {repo_id: 'someone/else'}) RETURN m",
    ],
)
def test_repo_id_may_only_scope_the_query(query):
    assert not validate_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        # CALLS is a relationship type, not the CALL keyword.
        "MATCH (a:Function {repo_id: $repo_id})-[:CALLS]->(b) RETURN b.name LIMIT 10",
        "MATCH (f:Function) WHERE f.repo_id = $repo_id RETURN f.name",
        "MATCH (f:Function) WHERE $repo_id = f.repo_id RETURN f.name",
        "MATCH (f:Function {repo_id: $repo_id}) WHERE f.name STARTS WITH 'test_' RETURN f",
        "MATCH (f:Function {repo_id: $repo_id}) WHERE f.docstring CONTAINS 'show' RETURN f",
        "MATCH (f:Function {repo_id: $repo_id}) WHERE f.name = 'use_cache' RETURN f",
    ],
)
def test_legitimate_reads_still_pass(query):
    assert validate_cypher(query), validate_cypher(query).error
