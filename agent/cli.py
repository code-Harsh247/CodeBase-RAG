"""Natural-language query commands."""

from __future__ import annotations

import sys

from agent.provider import get_provider
from agent.query_agent import QueryAgent
from graph.neo4j_client import Neo4jClient


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
        "--quiet",
        action="store_true",
        help="print only the answer, hiding the query and token usage",
    )

    return {"query": _cmd_query}


def _cmd_query(args) -> int:
    provider = get_provider(args.provider, args.model)

    with Neo4jClient.from_env() as client:
        client.verify_connectivity()
        result = QueryAgent(provider, client, args.repo_id).answer(args.question)

    if not args.quiet:
        # Showing the query and its result count is the evidence that the answer
        # is grounded rather than generated — a stated requirement, not debug output.
        for index, attempt in enumerate(result.attempts, start=1):
            label = "cypher" if attempt.ok else f"cypher (rejected, attempt {index})"
            print(f"--- {label} ---", file=sys.stderr)
            print(attempt.cypher, file=sys.stderr)
            if attempt.error:
                print(f"error: {attempt.error}", file=sys.stderr)
        if result.outcome:
            print(f"--- {result.outcome.row_count} rows ---", file=sys.stderr)
        usage = result.usage
        print(
            f"--- {usage.calls} LLM calls, {usage.total_tokens} tokens "
            f"({usage.reasoning_tokens} reasoning) ---",
            file=sys.stderr,
        )

    print(result.answer)
    return 0
