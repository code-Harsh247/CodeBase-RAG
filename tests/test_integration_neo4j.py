"""End-to-end ingestion against a live Neo4j. Skipped when none is reachable."""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from graph.neo4j_client import Neo4jClient
from graph.schema import SHARED_LABEL
from ingestion.pipeline import ingest


@pytest.fixture(scope="module")
def client():
    load_dotenv()
    try:
        connection = Neo4jClient.from_env()
        connection.verify_connectivity()
    except (ServiceUnavailable, Neo4jError, OSError, RuntimeError) as exc:
        pytest.skip(f"Neo4j not available: {exc}")
    yield connection
    connection.delete_repo("sample_repo")
    connection.close()


def test_ingest_loads_the_expected_graph(client, fixture_repo):
    summary = ingest(str(fixture_repo), client)

    assert summary.files_parsed == 6
    assert summary.files_failed == 0

    loaded = client.run(
        f"MATCH (n:{SHARED_LABEL} {{repo_id: $repo}}) RETURN count(n) AS count",
        repo=summary.repo_id,
    )
    assert loaded[0]["count"] == sum(summary.node_counts.values())


def test_reingestion_is_idempotent(client, fixture_repo):
    first = ingest(str(fixture_repo), client)
    second = ingest(str(fixture_repo), client)

    assert first.node_counts == second.node_counts
    assert first.edge_counts == second.edge_counts

    nodes = client.run(
        f"MATCH (n:{SHARED_LABEL} {{repo_id: $repo}}) RETURN count(n) AS count",
        repo=second.repo_id,
    )
    edges = client.run(
        f"MATCH (:{SHARED_LABEL} {{repo_id: $repo}})-[r]->() RETURN count(r) AS count",
        repo=second.repo_id,
    )
    assert nodes[0]["count"] == sum(second.node_counts.values())
    assert edges[0]["count"] == sum(second.edge_counts.values())


def test_callers_query_returns_real_answers(client, fixture_repo):
    summary = ingest(str(fixture_repo), client)

    rows = client.run(
        f"MATCH (caller)-[:CALLS]->(target:{SHARED_LABEL} {{repo_id: $repo}}) "
        f"WHERE target.qualified_name = 'pkg.models.Dog.speak' "
        f"RETURN caller.qualified_name AS caller ORDER BY caller",
        repo=summary.repo_id,
    )
    callers = [row["caller"] for row in rows]
    assert callers == [
        "pkg.guarded.describe",
        "pkg.models.Dog.fetch",
        "pkg.services.Kennel.add",
        "pkg.services.make_dog",
    ]


# ------------------------------------------------- bounded, read-only execution


def test_read_mode_refuses_writes_even_without_the_guard(client):
    # The last line of defence: if a write ever slipped past cypher_guard, the
    # READ-mode session makes the database itself refuse it.
    with pytest.raises(Neo4jError):
        client.run_read("CREATE (n:GuardProbe {x: 1}) RETURN n")
    assert client.run("MATCH (n:GuardProbe) RETURN count(n) AS c")[0]["c"] == 0


def test_run_read_fetches_at_most_one_row_past_the_cap(client):
    rows = client.run_read("UNWIND range(1, 10000) AS i RETURN i", max_rows=5)
    assert len(rows) == 6


def test_run_read_times_out(client):
    with pytest.raises(Neo4jError):
        client.run_read(
            "UNWIND range(1, 100000000) AS i WITH i WHERE i % 7 = 3 RETURN count(i)",
            timeout_s=0.5,
        )
