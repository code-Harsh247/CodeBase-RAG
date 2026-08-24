"""The `eval` command."""

from __future__ import annotations

import sys
from pathlib import Path

from agent.provider import get_provider
from evaluation.baseline import NaiveRAG
from evaluation.dataset import load_questions, summarise
from evaluation.report import render
from evaluation.runner import SYSTEMS, run_eval
from graph.neo4j_client import Neo4jClient
from ingestion.repo import DEFAULT_CLONE_ROOT

DEFAULT_QUESTIONS = Path("evaluation/questions/requests.json")


def register(subparsers) -> dict:
    parser = subparsers.add_parser(
        "eval", help="score the graph systems against a naive-RAG baseline"
    )
    parser.add_argument(
        "--questions", default=str(DEFAULT_QUESTIONS), help="ground-truth question file"
    )
    parser.add_argument("--provider", help="override LLM_PROVIDER")
    parser.add_argument("--model", help="override the provider's model")
    parser.add_argument(
        "--systems",
        default=",".join(SYSTEMS),
        help=f"comma-separated subset of: {', '.join(SYSTEMS)}",
    )
    parser.add_argument(
        "--limit", type=int, help="only run the first N questions (for a smoke test)"
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds between calls, to stay under a tokens-per-minute cap",
    )
    parser.add_argument("--repo-path", help="local checkout (default: .repos/<owner>/<name>)")
    parser.add_argument("--out", default="evaluation/results.json", help="where to write results")
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="reuse the existing baseline chunk index instead of rebuilding it",
    )
    return {"eval": _cmd_eval}


def _cmd_eval(args) -> int:
    questions = load_questions(args.questions)
    if args.limit:
        questions = questions[: args.limit]
    systems = tuple(item.strip() for item in args.systems.split(",") if item.strip())

    unknown = set(systems) - set(SYSTEMS)
    if unknown:
        print(f"Unknown system(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    repo_id = questions[0].repo_id
    repo_path = Path(args.repo_path) if args.repo_path else DEFAULT_CLONE_ROOT / repo_id
    if not repo_path.exists():
        print(
            f"No local checkout at {repo_path}. Run `ingest` for {repo_id} first.",
            file=sys.stderr,
        )
        return 2

    provider = get_provider(args.provider, args.model)
    print(f"{summarise(questions)} | systems: {', '.join(systems)}", file=sys.stderr)
    print(f"model: {getattr(provider, 'model', '?')}", file=sys.stderr)

    if "baseline" in systems and not args.skip_index:
        # The baseline needs its own chunk index; the graph systems use the
        # ingested graph and its summary index.
        chunks = NaiveRAG(provider, repo_path).index(repo_id)
        print(f"baseline indexed {chunks} chunks", file=sys.stderr)

    with Neo4jClient.from_env() as client:
        client.verify_connectivity()
        run = run_eval(
            questions,
            provider,
            client,
            repo_path,
            systems=systems,
            pause_seconds=args.pause,
            save_to=args.out,
        )

    run.save(args.out)
    print(f"\nwrote {args.out}", file=sys.stderr)
    print()
    print(render(run, systems))
    return 0
