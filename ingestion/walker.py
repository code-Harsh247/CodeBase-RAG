"""Walking a checked-out repository to find source files worth parsing."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from ingestion.limits import IngestLimitError

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
    max_files: int | None = None,
    max_total_bytes: int | None = None,
) -> Iterator[Path]:
    """Yield source files under ``root`` matching ``extensions``.

    Paths are yielded in sorted order so ingestion is deterministic.

    Symlinks are never followed: a hostile repository could otherwise point
    ``config.py`` at a file elsewhere on the server and have it indexed and
    quoted back. ``max_files`` / ``max_total_bytes`` raise
    :class:`IngestLimitError` once exceeded, so an oversized repository fails
    fast instead of tying up the server.
    """
    root = Path(root)
    base = root.resolve()
    wanted = {ext.lower() for ext in extensions}
    skip = set(exclude_dirs)
    if not include_tests:
        skip |= TEST_DIR_NAMES

    count = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in wanted:
            continue
        rel_parts = path.relative_to(root).parts[:-1]
        if any(part in skip for part in rel_parts):
            continue
        # A symlinked *directory* above the file would also resolve elsewhere.
        if base not in path.resolve().parents:
            continue
        size = path.stat().st_size
        if size > max_file_bytes:
            continue

        count += 1
        total_bytes += size
        if max_files is not None and count > max_files:
            raise IngestLimitError(
                "repo_too_large", f"The repository has more than {max_files} Python files."
            )
        if max_total_bytes is not None and total_bytes > max_total_bytes:
            raise IngestLimitError(
                "repo_too_large",
                f"The repository has more than {max_total_bytes // 1_000_000} MB of Python source.",
            )
        yield path
