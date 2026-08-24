"""Unified command-line entry point.

Subcommands are contributed by the modules that own them, so ingestion and
querying stay separate concerns behind one command.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

import agent.cli
import evaluation.cli
import ingestion.cli


def _use_utf8_output() -> None:
    """Model output routinely contains characters the Windows console cannot encode.

    Without this, an answer containing a typographic space or dash crashes the
    CLI with UnicodeEncodeError on a default Windows terminal.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    parser = argparse.ArgumentParser(prog="codegraph", description="Codebase knowledge graph")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    handlers: dict = {}
    handlers.update(ingestion.cli.register(subparsers))
    handlers.update(agent.cli.register(subparsers))
    handlers.update(evaluation.cli.register(subparsers))
    return parser, handlers


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    load_dotenv()
    parser, handlers = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # "index already exists" on every re-ingest drowns out the summary.
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
