"""Natural-language query commands."""

from __future__ import annotations

import sys
from pathlib import Path

from agent.agent_loop import MultiHopAgent
from agent.provider import get_provider
from agent.query_agent import QueryAgent
from graph.neo4j_client import Neo4jClient
from ingestion.repo import DEFAULT_CLONE_ROOT
from retrieval.tools import RetrievalTools


def register(subparsers) -> dict:
    """Add query commands to a shared parser. Returns command -> handler."""
    query_parser = subparsers.add_parser(
        "query", help="ask a natural-language question about an ingested repo"
    )
    query_parser.add_argument("repo_id", help="e.g. psf/requests")
    query_parser.add_argument("question")
    query_parser.add_argument("--provider", help="override LLM_PROVIDER")
    query_parser.add_argument("--model", help="override LLM_MODEL")
    query_parser.add_argument(
        "--single-hop",
        action="store_true",
        help="use the single graph query path instead of multi-hop retrieval",
    )
    query_parser.add_argument(
        "--repo-path",
        help="local checkout to read source from (default: .repos/<owner>/<name>)",
    )
    query_parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only the answer, hiding the retrieval trace",
    )

    return {"query": _cmd_query}


def _default_repo_path(repo_id: str) -> Path | None:
    candidate = DEFAULT_CLONE_ROOT / repo_id
    return candidate if candidate.exists() else None


def _cmd_query(args) -> int:
    provider = get_provider(args.provider, args.model)

    with Neo4jClient.from_env() as client:
        client.verify_connectivity()
        if args.single_hop:
            return _run_single_hop(args, provider, client)
        return _run_multi_hop(args, provider, client)


def _run_single_hop(args, provider, client) -> int:
    result = QueryAgent(provider, client, args.repo_id).answer(args.question)

    if not args.quiet:
        for index, attempt in enumerate(result.attempts, start=1):
            label = "cypher" if attempt.ok else f"cypher (rejected, attempt {index})"
            print(f"--- {label} ---", file=sys.stderr)
            print(attempt.cypher, file=sys.stderr)
            if attempt.error:
                print(f"error: {attempt.error}", file=sys.stderr)
        if result.outcome:
            print(f"--- {result.outcome.row_count} rows ---", file=sys.stderr)
        _print_usage(result.usage)

    print(result.answer)
    return 0


def _run_multi_hop(args, provider, client) -> int:
    repo_path = Path(args.repo_path) if args.repo_path else _default_repo_path(args.repo_id)
    tools = RetrievalTools(client, args.repo_id, repo_path=repo_path)
    result = MultiHopAgent(provider, tools).answer(args.question)

    if not args.quiet:
        # The trace is the evidence the answer came from the codebase. Showing
        # which tools ran, in order, is a feature rather than debug output.
        for index, hop in enumerate(result.hops, start=1):
            print(f"--- hop {index}: {hop.summary()} ---", file=sys.stderr)
        if result.hit_hop_limit:
            print("--- hop limit reached ---", file=sys.stderr)
        _print_usage(result.usage)

    print(result.answer)
    return 0


def _print_usage(usage) -> None:
    print(
        f"--- {usage.calls} LLM calls, {usage.total_tokens} tokens "
        f"({usage.reasoning_tokens} reasoning) ---",
        file=sys.stderr,
    )
