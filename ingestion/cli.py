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
    ingest_parser.add_argument(
        "--public", action="store_true", help="show it to every user of the web app"
    )

    cypher_parser = subparsers.add_parser("cypher", help="run a read-only Cypher query")
    cypher_parser.add_argument("query")
    cypher_parser.add_argument("--limit", type=int, default=25, help="rows to print")

    stats_parser = subparsers.add_parser("stats", help="node and edge counts for a repo")
    stats_parser.add_argument("repo_id", help="e.g. owner/name")

    return {"ingest": _cmd_ingest, "cypher": _cmd_cypher, "stats": _cmd_stats}


def _cmd_ingest(args) -> int:
    with Neo4jClient.from_env() as client:
        client.verify_connectivity()
        summary = ingest(args.source, client, args.include_tests, args.refresh)
        print(summary.render())
    _register(summary, public=args.public)
    return 0


def _register(summary, public: bool) -> None:
    """Record a CLI-ingested repository so the web app can serve it.

    This is how demo repositories are seeded: ingest from the shell with
    ``--public`` and every signed-in user sees them.
    """
    from api.settings import Settings
    from api.store import AppStore
    from ingestion.repo import validate_repo_id

    try:
        validate_repo_id(summary.repo_id)
    except ValueError:
        print(f"(not registered with the web app: {summary.repo_id!r} is not owner/name)")
        return
    store = AppStore(Settings.from_env().app_db_path)
    try:
        store.mark_ready(
            summary.repo_id,
            commit_sha=summary.commit,
            files=summary.files_parsed,
            nodes=sum(summary.node_counts.values()),
            public=True if public else None,
        )
    finally:
        store.close()
    print(f"registered with the web app{' as public' if public else ''}")


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
