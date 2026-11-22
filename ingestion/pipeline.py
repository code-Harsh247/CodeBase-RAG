"""End-to-end ingestion: clone -> parse -> resolve -> load into Neo4j."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from graph.neo4j_client import Neo4jClient
from graph.schema import Edge, Node
from ingestion.languages import PYTHON, get_parser
from ingestion.limits import IngestLimitError, IngestLimits
from ingestion.python_mapper import PythonMapper, module_qname_for
from ingestion.repo import ClonedRepo, clone_repo, local_repo
from ingestion.resolver import Resolver
from ingestion.symbols import ParsedModule
from ingestion.walker import walk_files
from retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class IngestionSummary:
    repo_id: str
    commit: str
    files_parsed: int = 0
    files_failed: int = 0
    node_counts: dict[str, int] = field(default_factory=dict)
    edge_counts: dict[str, int] = field(default_factory=dict)
    embedded: int = 0
    resolution: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def render(self) -> str:
        lines = [
            f"repo:    {self.repo_id} @ {self.commit[:12]}",
            f"files:   {self.files_parsed} parsed, {self.files_failed} failed",
            f"elapsed: {self.duration_seconds:.1f}s",
            "nodes:",
        ]
        lines += [f"  {label:<10} {count}" for label, count in sorted(self.node_counts.items())]
        lines.append("edges:")
        lines += [f"  {label:<12} {count}" for label, count in sorted(self.edge_counts.items())]
        stats = self.resolution
        if stats:
            lines.append("resolution (rate = resolved / internal references):")
            for kind in ("calls", "bases", "type_refs"):
                total = stats.get(f"{kind}_total", 0)
                resolved = stats.get(f"{kind}_resolved", 0)
                skipped = stats.get(f"{kind}_external", 0) + stats.get(
                    f"{kind}_unknown_receiver", 0
                )
                unresolved = total - resolved - skipped
                internal = resolved + unresolved
                rate = f"{resolved / internal:.0%}" if internal else "n/a"
                lines.append(
                    f"  {kind:<10} {resolved} resolved, {unresolved} unresolved, "
                    f"{skipped} out-of-scope  ({rate})"
                )
            internal, total = stats.get("imports_internal", 0), stats.get("imports_total", 0)
            lines.append(f"  {'imports':<10} {internal}/{total} internal")
        lines.append(f"embedded: {self.embedded} definitions for semantic search")
        return "\n".join(lines)


def parse_repository(
    repo: ClonedRepo, include_tests: bool = False, limits: IngestLimits | None = None
) -> tuple[list[ParsedModule], int]:
    """Parse every supported source file in ``repo``. Returns (modules, failures)."""
    limits = limits or IngestLimits.unbounded()
    parser = get_parser(PYTHON.name)
    modules: list[ParsedModule] = []
    failures = 0

    files = walk_files(
        repo.path,
        PYTHON.extensions,
        include_tests=include_tests,
        max_files=limits.max_files,
        max_total_bytes=limits.max_total_bytes,
    )
    for path in files:
        limits.check()
        rel_path = path.relative_to(repo.path).as_posix()
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
            qname = module_qname_for(repo.path, path)
            modules.append(PythonMapper(repo.repo_id, source, rel_path, qname).run(tree))
        except Exception:  # a single unparseable file must not abort ingestion
            logger.warning("failed to parse %s", rel_path, exc_info=True)
            failures += 1

    return modules, failures


def _dedupe_nodes(nodes: list[Node]) -> list[Node]:
    """Collapse nodes sharing an id, keeping the last.

    Neo4j MERGEs duplicates silently, so this mattered only once the vector
    store rejected them. Overload stubs are filtered earlier; this stays as a
    guard so an unforeseen duplicate degrades the graph slightly instead of
    aborting ingestion. The last definition wins because, for same-named
    definitions in a file, it is the one that is live at import time.
    """
    unique: dict[str, Node] = {}
    for node in nodes:
        unique[node.id] = node
    dropped = len(nodes) - len(unique)
    if dropped:
        logger.warning("collapsed %d duplicate node id(s)", dropped)
    return list(unique.values())


def _dedupe_edges(edges: list[Edge]) -> list[Edge]:
    """Collapse repeated (source, target, type) edges, keeping a count."""
    merged: dict[tuple[str, str, str], Edge] = {}
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for edge in edges:
        key = (edge.source_id, edge.target_id, edge.type.value)
        counts[key] += 1
        if key not in merged:
            merged[key] = edge
    for key, edge in merged.items():
        if counts[key] > 1:
            edge.properties["occurrences"] = counts[key]
    return list(merged.values())


def ingest(
    source: str,
    client: Neo4jClient,
    include_tests: bool = False,
    refresh: bool = False,
    vector_store: VectorStore | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    allow_local: bool = True,
    limits: IngestLimits | None = None,
) -> IngestionSummary:
    """Ingest a GitHub URL or a local directory path into the graph.

    ``allow_local=False`` treats ``source`` strictly as a GitHub URL. The web
    server passes it: otherwise a request whose "URL" happened to name an
    existing directory on the server would have that directory indexed.

    ``on_progress(stage, detail)`` is called as each stage begins. Ingestion
    takes tens of seconds and its slowest parts (cloning, embedding) give no
    output of their own, so a caller driving a UI needs to be told what is
    happening rather than showing an unexplained wait.

    ``limits`` bounds the work (see :class:`IngestLimits`); breaking one raises
    :class:`IngestLimitError`. The graph and index for the repo may then be
    partly rebuilt, so the caller cleans up on failure.
    """
    started = time.perf_counter()
    report = on_progress or (lambda stage, detail: None)
    limits = limits or IngestLimits.unbounded()

    path = Path(source)
    report("clone", f"fetching {source}")
    if allow_local and path.exists():
        repo = local_repo(path)
    else:
        repo = clone_repo(
            source,
            refresh=refresh,
            timeout_s=limits.clone_timeout_s,
            max_disk_bytes=limits.max_clone_disk_bytes,
        )
    logger.info("ingesting %s from %s", repo.repo_id, repo.path)

    report("parse", f"parsing {repo.repo_id}")
    modules, failures = parse_repository(repo, include_tests=include_tests, limits=limits)
    if not modules:
        raise IngestLimitError("no_python_files", "That repository has no Python source to index.")
    limits.check()

    report("resolve", f"resolving references across {len(modules)} modules")
    resolution = Resolver(modules).resolve()

    nodes: list[Node] = []
    edges: list[Edge] = []
    for module in modules:
        nodes.extend(module.fragment.nodes)
        edges.extend(module.fragment.edges)
    edges.extend(resolution.edges)

    for node in nodes:
        updates = resolution.node_updates.get(node.id)
        if updates:
            node.properties.update(updates)
    nodes = _dedupe_nodes(nodes)
    if limits.max_graph_nodes is not None and len(nodes) > limits.max_graph_nodes:
        raise IngestLimitError(
            "repo_too_large",
            f"The repository would produce more than {limits.max_graph_nodes:,} graph nodes.",
        )
    limits.check()

    report("load", f"writing {len(nodes)} nodes to the graph")
    client.ensure_schema()
    client.delete_repo(repo.repo_id)
    node_counts = client.load_nodes(nodes)
    edge_counts = client.load_edges(_dedupe_edges(edges))

    report("embed", "building the semantic index")
    store = vector_store if vector_store is not None else VectorStore()
    limits.check()
    embedded = store.index(repo.repo_id, nodes, check=limits.check)

    return IngestionSummary(
        repo_id=repo.repo_id,
        commit=repo.commit,
        files_parsed=len(modules),
        files_failed=failures,
        node_counts=node_counts,
        edge_counts=edge_counts,
        resolution=resolution.stats.as_dict(),
        embedded=embedded,
        duration_seconds=time.perf_counter() - started,
    )
