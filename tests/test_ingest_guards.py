"""Limits on what the server will clone and index."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from ingestion import repo as repo_module
from ingestion.github import fetch_repo_meta
from ingestion.limits import IngestLimitError, IngestLimits
from ingestion.pipeline import ingest, parse_repository
from ingestion.repo import ClonedRepo, clone_repo, local_repo
from retrieval.vector_store import VectorStore


class FakeGit:
    """Records git invocations; `clone` writes ``files`` into the target dir."""

    def __init__(self, files=None, returncode=0, stderr="", hang=False):
        self.files = files if files is not None else {"pkg/mod.py": "x = 1\n"}
        self.returncode = returncode
        self.stderr = stderr
        self.hang = hang
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if "clone" in command:
            if self.hang:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            target = Path(command[-1])
            if self.returncode == 0:
                (target / ".git").mkdir(parents=True)
                for name, text in self.files.items():
                    (target / name).parent.mkdir(parents=True, exist_ok=True)
                    (target / name).write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        raise AssertionError(f"unexpected git call: {command}")

    @property
    def clone(self) -> dict:
        return next(call for call in self.calls if "clone" in call["command"])


@pytest.fixture
def fake_git(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(repo_module.subprocess, "run", git)
    return git


# ------------------------------------------------------------------ clone


def test_clone_is_hardened(fake_git, tmp_path):
    cloned = clone_repo("https://github.com/psf/requests", root=tmp_path, timeout_s=42)

    command = fake_git.clone["command"]
    assert "core.symlinks=false" in command
    assert "protocol.file.allow=never" in command
    for flag in ("--depth=1", "--single-branch", "--no-tags"):
        assert flag in command
    # `--` before the URL: nothing user-derived can be read as an option.
    assert command[command.index("--") + 1] == "https://github.com/psf/requests.git"
    assert "--recurse-submodules" not in command
    env = fake_git.clone["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert fake_git.clone["timeout"] == 42

    assert cloned.commit == "abc123"
    assert (tmp_path / "psf" / "requests" / "pkg" / "mod.py").exists()
    assert not list((tmp_path / "psf").glob(".*clone-*")), "staging dir left behind"


def test_clone_timeout_leaves_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(repo_module.subprocess, "run", FakeGit(hang=True))
    with pytest.raises(IngestLimitError) as caught:
        clone_repo("https://github.com/a/b", root=tmp_path, timeout_s=1)
    assert caught.value.code == "timeout"
    assert not (tmp_path / "a" / "b").exists()
    assert not list((tmp_path / "a").glob(".*"))


def test_missing_repo_is_reported_as_not_found(monkeypatch, tmp_path):
    git = FakeGit(returncode=128, stderr="remote: Repository not found.")
    monkeypatch.setattr(repo_module.subprocess, "run", git)
    with pytest.raises(IngestLimitError) as caught:
        clone_repo("https://github.com/a/b", root=tmp_path)
    assert caught.value.code == "repo_not_found"


def test_other_git_failures_are_clone_failed_without_stderr(monkeypatch, tmp_path):
    git = FakeGit(returncode=128, stderr="fatal: unable to access /home/ubuntu/secret")
    monkeypatch.setattr(repo_module.subprocess, "run", git)
    with pytest.raises(IngestLimitError) as caught:
        clone_repo("https://github.com/a/b", root=tmp_path)
    assert caught.value.code == "clone_failed"
    assert "/home" not in caught.value.message


def test_oversized_checkout_is_refused_and_removed(monkeypatch, tmp_path):
    git = FakeGit(files={"big.py": "x" * 5_000})
    monkeypatch.setattr(repo_module.subprocess, "run", git)
    with pytest.raises(IngestLimitError) as caught:
        clone_repo("https://github.com/a/b", root=tmp_path, max_disk_bytes=1_000)
    assert caught.value.code == "repo_too_large"
    assert not (tmp_path / "a" / "b").exists()
    assert not list((tmp_path / "a").glob(".*"))


def test_refresh_replaces_the_checkout(fake_git, tmp_path):
    clone_repo("https://github.com/a/b", root=tmp_path)
    (tmp_path / "a" / "b" / "stale.py").write_text("old", encoding="utf-8")
    clone_repo("https://github.com/a/b", root=tmp_path, refresh=True)
    assert not (tmp_path / "a" / "b" / "stale.py").exists()


def test_existing_checkout_is_reused(fake_git, tmp_path):
    clone_repo("https://github.com/a/b", root=tmp_path)
    clone_repo("https://github.com/a/b", root=tmp_path)
    assert sum("clone" in call["command"] for call in fake_git.calls) == 1


def test_plain_folder_inside_another_checkout_is_not_that_checkout(tmp_path):
    # `git -C` searches parents; local_repo must not borrow an outer HEAD.
    assert local_repo(tmp_path).commit == "local"


# ----------------------------------------------------------------- limits


def _repo(tmp_path, files: dict[str, str]) -> ClonedRepo:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return ClonedRepo(repo_id="a/b", url="", path=tmp_path, commit="x")


def test_file_count_limit(tmp_path):
    repo = _repo(tmp_path, {f"m{i}.py": "x = 1\n" for i in range(5)})
    with pytest.raises(IngestLimitError) as caught:
        parse_repository(repo, limits=IngestLimits(max_files=3))
    assert caught.value.code == "repo_too_large"


def test_deadline_stops_parsing(tmp_path):
    repo = _repo(tmp_path, {"m.py": "x = 1\n"})
    with pytest.raises(IngestLimitError) as caught:
        parse_repository(repo, limits=IngestLimits(deadline=time.monotonic() - 1))
    assert caught.value.code == "timeout"


def test_cancel_stops_parsing(tmp_path):
    repo = _repo(tmp_path, {"m.py": "x = 1\n"})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(IngestLimitError) as caught:
        parse_repository(repo, limits=IngestLimits(cancel=cancel))
    assert caught.value.code == "cancelled"


class _NoGraph:
    def ensure_schema(self):
        raise AssertionError("must stop before touching the graph")


def test_repo_without_python_is_refused(tmp_path):
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    with pytest.raises(IngestLimitError) as caught:
        ingest(str(tmp_path), _NoGraph())
    assert caught.value.code == "no_python_files"


def test_graph_node_limit(tmp_path):
    (tmp_path / "m.py").write_text(
        "\n".join(f"def f{i}():\n    pass\n" for i in range(20)), encoding="utf-8"
    )
    with pytest.raises(IngestLimitError) as caught:
        ingest(str(tmp_path), _NoGraph(), limits=IngestLimits(max_graph_nodes=5))
    assert caught.value.code == "repo_too_large"


# ------------------------------------------------------------ github meta


def _api(status: int, payload: dict | None = None):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload or {}))


def test_github_meta_gives_the_canonical_name_and_size():
    payload = {"full_name": "psf/requests", "size": 9000, "private": False}
    meta = fetch_repo_meta("PSF", "Requests", transport=_api(200, payload))
    assert meta.full_name == "psf/requests"
    assert meta.size_kb == 9000


@pytest.mark.parametrize(
    ("status", "payload"), [(404, {"message": "Not Found"}), (200, {"full_name": "a/b", "private": True})]
)
def test_missing_or_private_repos_are_not_found(status, payload):
    with pytest.raises(IngestLimitError) as caught:
        fetch_repo_meta("a", "b", transport=_api(status, payload))
    assert caught.value.code == "repo_not_found"


@pytest.mark.parametrize("status", [403, 500, 502])
def test_github_trouble_fails_open(status):
    assert fetch_repo_meta("a", "b", transport=_api(status)) is None


def test_the_app_token_is_sent_when_configured():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"full_name": "a/b", "size": 1})

    fetch_repo_meta("a", "b", token="tok", transport=httpx.MockTransport(handler))
    assert seen == ["Bearer tok"]


# ------------------------------------------------------------ chroma names


def test_collection_names_no_longer_collide():
    name = VectorStore._collection_name
    assert name("u/my.lib") != name("u/my_lib")
    assert name("PSF/Requests") == name("psf/requests")
    assert len(name("a" * 39 + "/" + "b" * 100)) <= 63
