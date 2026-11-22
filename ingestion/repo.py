"""Cloning public GitHub repositories for ingestion."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ingestion.limits import IngestLimitError

logger = logging.getLogger(__name__)

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




# --------------------------------------------------------------------- clone

#: Every clone runs with these. The repository is untrusted input to `git`:
#: - core.symlinks=false checks symlinks out as plain files holding the link
#:   text, so nothing in the checkout can point elsewhere on the server;
#: - protocol.file.allow=never stops any local-path fetch (e.g. via submodule
#:   config) — submodules are not fetched at all, since there is no
#:   --recurse-submodules;
#: - --depth=1 --single-branch --no-tags fetches one commit and nothing else.
_GIT_CONFIG = ("-c", "core.symlinks=false", "-c", "protocol.file.allow=never")

_GIT_ENV = {
    # Never block waiting for credentials: a private or missing repo fails.
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    # LFS objects can be gigabytes; pointers are enough to skip them.
    "GIT_LFS_SKIP_SMUDGE": "1",
    # Ignore any system-wide git configuration on the server.
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git_env() -> dict[str, str]:
    return {**os.environ, **_GIT_ENV}


def _force_remove(func, path, _exc) -> None:
    """Clear the read-only bit and retry.

    Git marks files under `.git/objects` read-only, which makes `rmtree` fail
    on Windows. Ignoring errors instead would leave the clone on disk and
    report success, which is worse than failing loudly.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    # `onexc` replaced `onerror` in Python 3.12; the handler fits either.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def tree_size(path: Path) -> int:
    """Bytes on disk under ``path``, not following symlinks."""
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def head_commit(path: Path) -> str | None:
    """The checked-out commit of the git repository *at* ``path``, if any.

    `git -C` would happily search parent directories, so a plain folder inside
    some other checkout would report that checkout's HEAD; require ``.git`` here.
    """
    if not (Path(path) / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def clone_repo(
    url: str,
    root: Path | None = None,
    refresh: bool = False,
    *,
    timeout_s: float | None = None,
    max_disk_bytes: int | None = None,
) -> ClonedRepo:
    """Shallow-clone ``url`` under ``root``, reusing an existing checkout.

    The clone goes to a temporary sibling directory and is moved into place
    only once it is complete and within limits, so a failed or concurrent
    clone never leaves a half-written checkout where the agent will read it.
    ``refresh=True`` replaces an existing checkout.

    Raises :class:`IngestLimitError` with code ``repo_not_found``,
    ``clone_failed``, ``timeout`` or ``repo_too_large``.
    """
    owner, name = parse_github_url(url)
    repo_id = f"{owner}/{name}"
    dest = repo_dir(repo_id, root)

    if dest.is_dir() and not refresh:
        commit = head_commit(dest)
        if commit:
            return ClonedRepo(repo_id=repo_id, url=url, path=dest, commit=commit)
        remove_tree(dest)  # not a usable checkout; clone it afresh

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{name}.clone-{secrets.token_hex(4)}"
    command = [
        "git",
        *_GIT_CONFIG,
        "clone",
        "--depth=1",
        "--single-branch",
        "--no-tags",
        "--quiet",
        "--",
        f"https://github.com/{owner}/{name}.git",
        str(staging),
    ]
    try:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=_git_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise IngestLimitError(
                "timeout", "Downloading the repository took too long."
            ) from None
        if result.returncode != 0:
            stderr = result.stderr.lower()
            logger.warning("git clone of %s failed: %s", repo_id, result.stderr.strip()[:500])
            if "not found" in stderr or "could not read username" in stderr:
                raise IngestLimitError(
                    "repo_not_found",
                    "Repository not found. Check the URL; private repositories are not supported.",
                )
            raise IngestLimitError("clone_failed", "Could not download the repository from GitHub.")

        if max_disk_bytes is not None and tree_size(staging) > max_disk_bytes:
            raise IngestLimitError(
                "repo_too_large",
                f"The repository is larger than {max_disk_bytes // 1_000_000} MB on disk.",
            )

        commit = head_commit(staging) or "unknown"
        remove_tree(dest)
        os.replace(staging, dest)
    finally:
        remove_tree(staging)

    return ClonedRepo(repo_id=repo_id, url=url, path=dest, commit=commit)


def local_repo(path: Path, repo_id: str | None = None) -> ClonedRepo:
    """Wrap an already-local directory as a :class:`ClonedRepo` (useful for tests)."""
    path = Path(path).resolve()
    # Not a git checkout, or one with no commits yet: still ingestible.
    commit = head_commit(path) or "local"
    return ClonedRepo(repo_id=repo_id or path.name, url=str(path), path=path, commit=commit)
