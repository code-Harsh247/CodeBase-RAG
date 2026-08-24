"""Walking a checked-out repository to find source files worth parsing."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

#: Directories that never contain first-party source worth indexing.
DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        "vendor",
        "third_party",
    }
)

#: Skipped unless ``include_tests=True`` — tests inflate the graph with fixtures
#: and duplicate call paths that rarely help answer questions about the codebase.
TEST_DIR_NAMES = frozenset({"test", "tests", "testing", "__tests__", "spec", "__mocks__"})


def walk_files(
    root: Path,
    extensions: Iterable[str],
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    include_tests: bool = False,
    max_file_bytes: int = 2_000_000,
) -> Iterator[Path]:
    """Yield source files under ``root`` matching ``extensions``.

    Paths are yielded in sorted order so ingestion is deterministic.
    """
    root = Path(root)
    wanted = {ext.lower() for ext in extensions}
    skip = set(exclude_dirs)
    if not include_tests:
        skip |= TEST_DIR_NAMES

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in wanted:
            continue
        rel_parts = path.relative_to(root).parts[:-1]
        if any(part in skip for part in rel_parts):
            continue
        if path.stat().st_size > max_file_bytes:
            continue
        yield path
