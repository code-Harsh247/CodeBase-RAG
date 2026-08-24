"""Neo4j connection and bulk loading.

Every node carries the shared ``CodeNode`` label plus its specific type label, so
one uniqueness constraint and one index serve all id lookups.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Self

from neo4j import GraphDatabase

from graph.schema import SHARED_LABEL, Edge, EdgeType, Node, NodeType

_CONSTRAINTS = (
    (
        f"CREATE CONSTRAINT code_node_id IF NOT EXISTS "
        f"FOR (n:{SHARED_LABEL}) REQUIRE n.id IS UNIQUE"
    ),
    f"CREATE INDEX code_node_repo IF NOT EXISTS FOR (n:{SHARED_LABEL}) ON (n.repo_id)",
    f"CREATE INDEX code_node_qname IF NOT EXISTS FOR (n:{SHARED_LABEL}) ON (n.qualified_name)",
    f"CREATE INDEX code_node_name IF NOT EXISTS FOR (n:{SHARED_LABEL}) ON (n.name)",
)

_VALID_LABELS = {member.value for member in NodeType}
_VALID_EDGE_TYPES = {member.value for member in EdgeType}


def _clean(properties: dict) -> dict:
    """Neo4j cannot store nulls; drop them rather than writing empty strings."""
    return {key: value for key, value in properties.items() if value is not None}


class Neo4jClient:
    def __init__(self, uri: str, user: str, password: str, database: str | None = None) -> None:
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            user=os.environ.get("NEO4J_USER", "neo4j"),
            password=os.environ.get("NEO4J_PASSWORD", "changeme123"),
            database=os.environ.get("NEO4J_DATABASE") or None,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._driver.close()

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    def run(self, query: str, **params) -> list[dict]:
        with self._driver.session(database=self._database) as session:
            return [record.data() for record in session.run(query, **params)]

    # ---------------------------------------------------------------- schema

    def ensure_schema(self) -> None:
        for statement in _CONSTRAINTS:
            self.run(statement)

    def delete_repo(self, repo_id: str, batch_size: int = 5_000) -> int:
        """Remove every node for ``repo_id`` so re-ingestion cannot leave stale data."""
        deleted = 0
        while True:
            rows = self.run(
                f"MATCH (n:{SHARED_LABEL} {{repo_id: $repo_id}}) WITH n LIMIT $limit "
                f"DETACH DELETE n RETURN count(n) AS deleted",
                repo_id=repo_id,
                limit=batch_size,
            )
            count = rows[0]["deleted"] if rows else 0
            deleted += count
            if count == 0:
                return deleted

    # ----------------------------------------------------------------- write

    def load_nodes(self, nodes: Iterable[Node], batch_size: int = 1_000) -> dict[str, int]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for node in nodes:
            grouped[node.type.value].append({"id": node.id, "props": _clean(node.properties)})

        counts: dict[str, int] = {}
        for label, rows in grouped.items():
            # Labels come from NodeType, never user input, but assert it explicitly
            # since the label cannot be passed as a query parameter.
            assert label in _VALID_LABELS, f"Unexpected node label: {label}"
            query = (
                f"UNWIND $rows AS row "
                f"MERGE (n:`{label}`:`{SHARED_LABEL}` {{id: row.id}}) "
                f"SET n += row.props"
            )
            for start in range(0, len(rows), batch_size):
                self.run(query, rows=rows[start : start + batch_size])
            counts[label] = len(rows)
        return counts

    def load_edges(self, edges: Sequence[Edge], batch_size: int = 1_000) -> dict[str, int]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for edge in edges:
            grouped[edge.type.value].append(
                {
                    "source": edge.source_id,
                    "target": edge.target_id,
                    "props": _clean(edge.properties),
                }
            )

        counts: dict[str, int] = {}
        for edge_type, rows in grouped.items():
            assert edge_type in _VALID_EDGE_TYPES, f"Unexpected edge type: {edge_type}"
            query = (
                f"UNWIND $rows AS row "
                f"MATCH (a:{SHARED_LABEL} {{id: row.source}}) "
                f"MATCH (b:{SHARED_LABEL} {{id: row.target}}) "
                f"MERGE (a)-[r:`{edge_type}`]->(b) "
                f"SET r += row.props"
            )
            for start in range(0, len(rows), batch_size):
                self.run(query, rows=rows[start : start + batch_size])
            counts[edge_type] = len(rows)
        return counts
