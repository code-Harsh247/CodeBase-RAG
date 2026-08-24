"""Command-line entry point for ingestion and raw graph inspection."""

from __future__ import annotations

import json
import sys

from graph.neo4j_client import Neo4jClient
from graph.schema import SHARED_LABEL
from ingestion.pipeline import ingest


def register(subparsers) -> dict:
    """Add ingestion commands to a shared parser. Returns command -> handler."""
    ingest_parser = subparsers.add_parser("ingest", help="ingest a GitHub URL or local directory")
    ingest_parser.add_argument("source", help="GitHub URL or path to a local checkout")
    ingest_parser.add_argument("--include-tests", action="store_true", help="index test directories")
    ingest_parser.add_argument("--refresh", action="store_true", help="re-clone if already present")

    cypher_parser = subparsers.add_parser("cypher", help="run a read-only Cypher query")
    cypher_parser.add_argument("query")
    cypher_parser.add_argument("--limit", type=int, default=25, help="rows to print")

    stats_parser = subparsers.add_parser("stats", help="node and edge counts for a repo")
    stats_parser.add_argument("repo_id", help="e.g. owner/name")

    return {"ingest": _cmd_ingest, "cypher": _cmd_cypher, "stats": _cmd_stats}


def _cmd_ingest(args) -> int:
    with Neo4jClient.from_env() as client:
        client.verify_connectivity()
        print(ingest(args.source, client, args.include_tests, args.refresh).render())
    return 0


def _cmd_cypher(args) -> int:
    with Neo4jClient.from_env() as client:
        rows = client.run(args.query)
    for row in rows[: args.limit]:
        print(json.dumps(row, default=str))
    print(f"({len(rows)} rows)", file=sys.stderr)
    return 0


def _cmd_stats(args) -> int:
    with Neo4jClient.from_env() as client:
        nodes = client.run(
            f"MATCH (n:{SHARED_LABEL} {{repo_id: $repo}}) "
            f"RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC",
            repo=args.repo_id,
        )
        edges = client.run(
            f"MATCH (a:{SHARED_LABEL} {{repo_id: $repo}})-[r]->() "
            f"RETURN type(r) AS type, count(*) AS count ORDER BY count DESC",
            repo=args.repo_id,
        )
    print("nodes:")
    for row in nodes:
        label = next((item for item in row["labels"] if item != SHARED_LABEL), SHARED_LABEL)
        print(f"  {label:<12} {row['count']}")
    print("edges:")
    for row in edges:
        print(f"  {row['type']:<12} {row['count']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Kept so `python -m ingestion.cli` still works; `cli.py` is the full entry point."""
    from cli import main as unified_main

    return unified_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
