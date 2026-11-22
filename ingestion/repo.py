"""Cloning public GitHub repositories for ingestion."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError

#: Kept for the CLIs; the server and anything new should call `clone_root()`,
#: which honours REPOS_DIR.
DEFAULT_CLONE_ROOT = Path(".repos")

_GITHUB_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)

#: GitHub's own rules: owners are alphanumeric with single hyphens, at most 39
#: characters, and never contain a dot — so an owner can never be `.` or `..`.
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@dataclass
class ClonedRepo:
    """A repository checked out on local disk, ready to parse."""

    repo_id: str
    url: str
    path: Path
    commit: str


def validate_repo_id(repo_id: str) -> tuple[str, str]:
    """Return ``(owner, name)`` if ``repo_id`` is a well-formed ``owner/name``.

    Repo ids become filesystem paths (``<root>/<owner>/<name>``), so this is
    the line between "a GitHub repository" and "any directory on the server":
    `..`, extra slashes and anything GitHub itself would not accept are refused.
    """
    if not isinstance(repo_id, str) or repo_id.count("/") != 1:
        raise ValueError(f"Not a valid repository id: {repo_id!r}")
    owner, name = repo_id.split("/")
    if not _OWNER.match(owner):
        raise ValueError(f"Not a valid GitHub owner: {owner!r}")
    if not _NAME.match(name) or name in {".", ".."} or name.lower().endswith(".git"):
        raise ValueError(f"Not a valid GitHub repository name: {name!r}")
    return owner, name


def clone_root() -> Path:
    """Where clones live: ``REPOS_DIR``, or ``.repos`` under the working directory."""
    return Path(os.environ.get("REPOS_DIR") or DEFAULT_CLONE_ROOT)


def repo_dir(repo_id: str, root: Path | None = None) -> Path:
    """The checkout directory for ``repo_id``, guaranteed to be inside the clone root.

    Validation already rules out traversal; the resolve-and-compare is a second,
    independent check so a future change to the id rules cannot reopen it.
    """
    owner, name = validate_repo_id(repo_id)
    base = (root or clone_root()).resolve()
    target = (base / owner / name).resolve()
    if base not in target.parents:
        raise ValueError(f"Repository path escapes the clone root: {repo_id!r}")
    return target


def parse_github_url(url: str) -> tuple[str, str]:
    """Return ``(owner, name)`` for a public GitHub URL."""
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise ValueError(f"Not a recognized GitHub repository URL: {url!r}")
    return validate_repo_id(f"{match.group('owner')}/{match.group('name')}")


def clone_repo(
    url: str,
    root: Path | None = None,
    refresh: bool = False,
) -> ClonedRepo:
    """Shallow-clone ``url`` under ``root``, reusing an existing checkout.

    Set ``refresh=True`` to discard and re-clone an existing checkout.
    """
    owner, name = parse_github_url(url)
    repo_id = f"{owner}/{name}"
    dest = repo_dir(repo_id, root)

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
