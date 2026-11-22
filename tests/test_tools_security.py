"""The retrieval tools take untrusted input from the model and from the repo."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from retrieval.tools import MAX_GREP_PATTERN, MAX_TOOL_TEXT, RetrievalTools, ToolResult


class _Graph:
    """Answers the read_code lookup with whatever file path it is given."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.read_calls: list[tuple] = []

    def run(self, query, **params):  # pragma: no cover - run_read is preferred
        raise AssertionError("fixed lookups must use run_read")

    def run_read(self, query, params=None, *, timeout_s=None, max_rows=None):
        self.read_calls.append((query, params, timeout_s, max_rows))
        return self.rows


def _tools(repo: Path, rows: list[dict] | None = None) -> RetrievalTools:
    return RetrievalTools(_Graph(rows), "owner/repo", repo_path=repo, vector_store=object())


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need developer mode or admin rights on this machine")


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("def handler():\n    return 'ok'\n", encoding="utf-8")
    return root


# -------------------------------------------------------------------- grep


def test_catastrophic_regex_is_stopped(repo, monkeypatch):
    monkeypatch.setenv("GREP_TIMEOUT_SECONDS", "0.5")
    (repo / "evil.py").write_text("a" * 35 + "b\n", encoding="utf-8")

    started = time.monotonic()
    result = _tools(repo).grep("(a|aa)+$")
    elapsed = time.monotonic() - started

    assert not result.ok
    assert "too expensive" in result.text
    assert elapsed < 2.5


def test_plain_word_grep_still_works(repo):
    result = _tools(repo).grep("handler")
    assert result.ok
    assert "main.py:1" in result.text


def test_regex_grep_still_works(repo):
    result = _tools(repo).grep(r"def \w+\(")
    assert result.ok
    assert "main.py:1" in result.text


def test_overlong_pattern_is_refused(repo):
    result = _tools(repo).grep("a" * (MAX_GREP_PATTERN + 1))
    assert not result.ok
    assert "too long" in result.text


def test_invalid_regex_is_reported(repo):
    result = _tools(repo).grep("(unclosed")
    assert not result.ok
    assert "Invalid regular expression" in result.text


def test_grep_does_not_follow_symlinks_out_of_the_repo(repo, tmp_path):
    secret = tmp_path / "secret.py"
    secret.write_text("API_KEY = 'hunter2'\n", encoding="utf-8")
    _symlink_or_skip(repo / "config.py", secret)

    result = _tools(repo).grep("hunter2")
    assert "config.py" not in result.text
    assert result.text.startswith("No matches")


# --------------------------------------------------------------- read_code


def test_read_code_by_path_cannot_escape(repo, tmp_path):
    (tmp_path / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")
    result = _tools(repo).read_code("../outside.py")
    assert not result.ok
    assert "SECRET" not in result.text


def test_read_code_refuses_a_graph_path_outside_the_repo(repo, tmp_path):
    # file_path in the graph came from ingesting an untrusted repository.
    (tmp_path / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")
    rows = [{"file": "../outside.py", "start": 1, "end": 1, "name": "x"}]

    result = _tools(repo, rows).read_code("x")
    assert not result.ok
    assert "SECRET" not in result.text


def test_read_code_does_not_follow_symlinks_out_of_the_repo(repo, tmp_path):
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'abc'\n", encoding="utf-8")
    _symlink_or_skip(repo / "link.py", secret)

    assert "TOKEN" not in _tools(repo).read_code("link.py").text
    rows = [{"file": "link.py", "start": 1, "end": 1, "name": "x"}]
    assert "TOKEN" not in _tools(repo, rows).read_code("x").text


def test_read_code_lookup_is_read_only_and_bounded(repo):
    rows = [{"file": "main.py", "start": 1, "end": 2, "name": "handler"}]
    tools = _tools(repo, rows)

    result = tools.read_code("handler")

    assert result.ok
    assert "def handler" in result.text
    _, _, timeout_s, max_rows = tools.client.read_calls[0]
    assert timeout_s and max_rows


# ------------------------------------------------------------------ caps


def test_tool_results_are_capped():
    result = ToolResult(True, "x" * (MAX_TOOL_TEXT * 3))
    assert len(result.text) < MAX_TOOL_TEXT + 100
    assert result.text.endswith("(output truncated)")


def test_render_rows_caps_long_lists():
    from retrieval.graph_query import render_rows

    text = render_rows([{"callers": list(range(500))}])
    assert "more" in text
    assert "499" not in text


def test_render_rows_reports_truncation():
    from retrieval.graph_query import render_rows

    text = render_rows([{"name": "a"}], truncated=True)
    assert "more rows omitted" in text


def test_graph_query_marks_truncated_results(repo, monkeypatch):
    monkeypatch.setenv("NEO4J_MAX_ROWS", "3")
    rows = [{"name": f"f{i}"} for i in range(10)]
    tools = _tools(repo, rows)

    result = tools.graph_query("MATCH (f:Function {repo_id: $repo_id}) RETURN f.name AS name")

    assert result.ok
    assert result.text.startswith("3+ rows")
    assert "more rows omitted" in result.text
