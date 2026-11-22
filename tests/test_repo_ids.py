from __future__ import annotations

import pytest

from ingestion.repo import parse_github_url, repo_dir, validate_repo_id


@pytest.mark.parametrize(
    "repo_id",
    ["psf/requests", "pallets/click", "a/b", "some-org/my.lib", "u/my_lib-2", "x/.github"],
)
def test_accepts_real_repo_ids(repo_id):
    owner, name = validate_repo_id(repo_id)
    assert f"{owner}/{name}" == repo_id


@pytest.mark.parametrize(
    "repo_id",
    [
        "../../etc",
        "owner/..",
        "owner/.",
        "../owner",
        "owner/name/extra",
        "owner",
        "",
        "-owner/name",
        "owner/name.git",
        "owner/na me",
        r"owner\name",
        r"owner/name\..\..",
        "C:/Windows",
        "owner/" + "x" * 101,
    ],
)
def test_rejects_traversal_and_malformed_ids(repo_id):
    with pytest.raises(ValueError):
        validate_repo_id(repo_id)


def test_repo_dir_stays_inside_the_clone_root(tmp_path):
    target = repo_dir("psf/requests", root=tmp_path)
    assert tmp_path.resolve() in target.parents


def test_repo_dir_refuses_traversal(tmp_path):
    with pytest.raises(ValueError):
        repo_dir("../escape", root=tmp_path)


def test_repo_dir_honours_repos_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path))
    assert repo_dir("psf/requests") == (tmp_path / "psf" / "requests").resolve()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/..",
        "https://github.com/../etc",
        "https://evil.example.com/owner/name",
        "file:///etc/passwd",
    ],
)
def test_parse_github_url_rejects_bad_input(url):
    with pytest.raises(ValueError):
        parse_github_url(url)


def test_parse_github_url_strips_dot_git():
    assert parse_github_url("https://github.com/psf/requests.git") == ("psf", "requests")
