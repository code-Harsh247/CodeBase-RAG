"""Ask GitHub about a repository before cloning it.

One API call answers three questions a clone would only answer after
downloading everything: does it exist (and is it public), how big is it, and
what is its canonical ``owner/name`` spelling. GitHub names are
case-insensitive, so ``PSF/Requests`` and ``psf/requests`` must not become two
separate indexes.

Uses an app-level token (no scopes; only public data is read) when one is
configured, for 5,000 requests an hour instead of 60. Users' own GitHub tokens
are never stored, so they are not used here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ingestion.limits import IngestLimitError
from ingestion.repo import validate_repo_id

logger = logging.getLogger(__name__)

API = "https://api.github.com/repos/{owner}/{name}"


@dataclass(frozen=True)
class RepoMeta:
    #: Canonical ``owner/name`` as GitHub spells it.
    full_name: str
    #: GitHub's size estimate for the repository, in kilobytes.
    size_kb: int
    default_branch: str


def fetch_repo_meta(
    owner: str,
    name: str,
    *,
    token: str = "",
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = 10,
) -> RepoMeta | None:
    """Metadata for a public repository.

    Raises :class:`IngestLimitError` (``repo_not_found``) when GitHub says it
    does not exist or is private. Returns ``None`` when GitHub could not be
    asked (rate limit, outage, network): the clone's own limits still apply,
    so failing open here costs some efficiency, not safety.
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(transport=transport, timeout=timeout_s) as http:
            response = http.get(API.format(owner=owner, name=name), headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("GitHub metadata lookup failed for %s/%s: %s", owner, name, exc)
        return None

    if response.status_code == 404:
        raise IngestLimitError(
            "repo_not_found",
            "Repository not found. Check the URL; private repositories are not supported.",
        )
    if response.status_code != 200:
        logger.warning(
            "GitHub metadata lookup for %s/%s returned %d", owner, name, response.status_code
        )
        return None

    try:
        data = response.json()
        if data.get("private"):
            raise IngestLimitError(
                "repo_not_found",
                "Repository not found. Check the URL; private repositories are not supported.",
            )
        full_name = str(data["full_name"])
        validate_repo_id(full_name)
        return RepoMeta(
            full_name=full_name,
            size_kb=int(data.get("size") or 0),
            default_branch=str(data.get("default_branch") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("unexpected GitHub metadata for %s/%s: %s", owner, name, exc)
        return None
