"""Cloning public GitHub repositories for ingestion."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError

DEFAULT_CLONE_ROOT = Path(".repos")

_GITHUB_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)


@dataclass
class ClonedRepo:
    """A repository checked out on local disk, ready to parse."""

    repo_id: str
    url: str
    path: Path
    commit: str


def parse_github_url(url: str) -> tuple[str, str]:
    """Return ``(owner, name)`` for a public GitHub URL."""
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise ValueError(f"Not a recognized GitHub repository URL: {url!r}")
    return match.group("owner"), match.group("name")


def clone_repo(
    url: str,
    clone_root: Path = DEFAULT_CLONE_ROOT,
    refresh: bool = False,
) -> ClonedRepo:
    """Shallow-clone ``url`` into ``clone_root``, reusing an existing checkout.

    Set ``refresh=True`` to discard and re-clone an existing checkout.
    """
    owner, name = parse_github_url(url)
    repo_id = f"{owner}/{name}"
    dest = clone_root / owner / name

    if dest.exists() and refresh:
        shutil.rmtree(dest)

    if dest.exists():
        repo = Repo(dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        repo = Repo.clone_from(f"https://github.com/{owner}/{name}.git", dest, depth=1)

    return ClonedRepo(repo_id=repo_id, url=url, path=dest, commit=repo.head.commit.hexsha)


def local_repo(path: Path, repo_id: str | None = None) -> ClonedRepo:
    """Wrap an already-local directory as a :class:`ClonedRepo` (useful for tests)."""
    path = Path(path).resolve()
    try:
        commit = Repo(path).head.commit.hexsha
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError):
        # Not a git checkout, or a checkout with no commits yet.
        commit = "local"
    return ClonedRepo(repo_id=repo_id or path.name, url=str(path), path=path, commit=commit)
