from __future__ import annotations

import os

import pytest

from ingestion.limits import IngestLimitError
from ingestion.walker import walk_files


def _files(root, count: int, size: int = 10) -> None:
    for i in range(count):
        (root / f"m{i}.py").write_text("x" * size, encoding="utf-8")


def test_too_many_files_is_refused(tmp_path):
    _files(tmp_path, 5)
    with pytest.raises(IngestLimitError) as caught:
        list(walk_files(tmp_path, [".py"], max_files=3))
    assert caught.value.code == "repo_too_large"


def test_too_many_bytes_is_refused(tmp_path):
    _files(tmp_path, 3, size=1_000)
    with pytest.raises(IngestLimitError):
        list(walk_files(tmp_path, [".py"], max_total_bytes=2_500))


def test_within_limits_walks_everything(tmp_path):
    _files(tmp_path, 3)
    assert len(list(walk_files(tmp_path, [".py"], max_files=3, max_total_bytes=30))) == 3


def test_symlinked_files_are_not_indexed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("x = 1\n", encoding="utf-8")
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'abc'\n", encoding="utf-8")
    try:
        os.symlink(secret, repo / "config.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need developer mode or admin rights on this machine")

    names = [path.name for path in walk_files(repo, [".py"])]
    assert names == ["real.py"]
