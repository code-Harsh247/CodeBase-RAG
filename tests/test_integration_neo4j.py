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
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
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
