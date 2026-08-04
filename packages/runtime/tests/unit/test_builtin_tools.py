"""Unit tests for built-in workspace tools (read/write/glob/grep/edit/bash).

Since plan F1 (2026-08) the workspace is BOUND at construction time —
``build_builtin_tools(workspace)`` — and the model never sees a workspace
parameter. These tests drive tools built per workspace; the hardening cases
at the bottom pin the incident behaviour: a bad path yields an ``[error]``
tool result, never an exception that kills the turn.
"""

from __future__ import annotations

import pytest

from ginno_runtime.tools.builtin import build_builtin_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def ws(tmp_path):
    return str(tmp_path)


@pytest.fixture
def tools(ws):
    read_file, write_file, glob_files, grep_files, edit_file, bash = build_builtin_tools(ws)
    return {t.name: t for t in (read_file, write_file, glob_files, grep_files, edit_file, bash)}


def test_write_to_unwritable_path_returns_error_not_raise(tools, tmp_path):
    # parent is a FILE -> mkdir must fail; write_file should return [error], not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    res = tools["write_file"].invoke({"path": str(blocker / "sub" / "x.md"), "content": "hi"})
    assert res.startswith("[error]")


def test_write_then_read_roundtrip(tools):
    tools["write_file"].invoke({"path": "a.txt", "content": "hello"})
    assert tools["read_file"].invoke({"path": "a.txt"}) == "hello"


def test_read_missing_file(tools):
    out = tools["read_file"].invoke({"path": "missing.txt"})
    assert "[error] file not found" in out


def test_write_creates_nested_dirs(tools):
    tools["write_file"].invoke({"path": "sub/dir/a.txt", "content": "x"})
    assert tools["read_file"].invoke({"path": "sub/dir/a.txt"}) == "x"


def test_glob_files(tools):
    tools["write_file"].invoke({"path": "one.md", "content": "1"})
    tools["write_file"].invoke({"path": "two.md", "content": "2"})
    tools["write_file"].invoke({"path": "three.txt", "content": "3"})
    out = tools["glob_files"].invoke({"pattern": "*.md"})
    assert "one.md" in out and "two.md" in out
    assert "three.txt" not in out


def test_glob_recursive_pattern(tools):
    tools["write_file"].invoke({"path": "skills/demo/SKILL.md", "content": "x"})
    out = tools["glob_files"].invoke({"pattern": "**/SKILL.md"})
    assert "skills/demo/SKILL.md" in out


def test_grep_files(tools):
    tools["write_file"].invoke({"path": "code.py", "content": "def foo():\n    return 42\n"})
    out = tools["grep_files"].invoke({"pattern": "return 42"})
    assert "code.py:2:" in out


def test_grep_no_match(tools):
    tools["write_file"].invoke({"path": "code.py", "content": "x = 1\n"})
    assert tools["grep_files"].invoke({"pattern": "zzz"}) == "(no matches)"


def test_edit_unique_match(tools):
    tools["write_file"].invoke({"path": "a.txt", "content": "foo bar foo"})
    # 'bar' is unique
    assert tools["edit_file"].invoke({"path": "a.txt", "old": "bar", "new": "BAZ"}) == "ok"
    assert tools["read_file"].invoke({"path": "a.txt"}) == "foo BAZ foo"


def test_edit_multiple_matches(tools):
    tools["write_file"].invoke({"path": "a.txt", "content": "foo foo"})
    out = tools["edit_file"].invoke({"path": "a.txt", "old": "foo", "new": "x"})
    assert "multiple matches" in out


def test_edit_not_found(tools):
    tools["write_file"].invoke({"path": "a.txt", "content": "foo"})
    out = tools["edit_file"].invoke({"path": "a.txt", "old": "zzz", "new": "x"})
    assert "not found" in out


def test_bash_captures_exit_and_output(tools):
    out = tools["bash"].invoke({"command": "echo hi; exit 3"})
    assert "[exit 3]" in out
    assert "hi" in out


def test_bash_timeout(tools):
    out = tools["bash"].invoke({"command": "sleep 5", "timeout": 1})
    assert "timeout" in out


def test_build_builtin_tools_returns_six(ws):
    names = {t.name for t in build_builtin_tools(ws)}
    assert names == {"read_file", "write_file", "glob_files", "grep_files", "edit_file", "bash"}


def test_workspace_not_in_tool_schemas(ws):
    """F1: the model must never see (or have to pass) a workspace param."""
    for t in build_builtin_tools(ws):
        assert "workspace" not in t.args, t.name


def test_bash_cwd_is_bound_workspace(ws):
    out = build_builtin_tools(ws)[5].invoke({"command": "pwd"})
    # pwd output is the workspace, not the sidecar/process cwd
    assert ws in out


# --------------------------------------------------------------------------- #
# Hardening — incident 2026-08-04: OSError from glob_files killed the turn.
# Every tool must degrade to an [error] result, never raise.
# --------------------------------------------------------------------------- #
def test_glob_missing_workspace_dir_returns_error(tmp_path):
    tools = {t.name: t for t in build_builtin_tools(str(tmp_path / "does-not-exist"))}
    out = tools["glob_files"].invoke({"pattern": "**/skills/**"})
    assert out.startswith("[error]")


def test_grep_missing_workspace_dir_returns_error(tmp_path):
    tools = {t.name: t for t in build_builtin_tools(str(tmp_path / "nope"))}
    assert tools["grep_files"].invoke({"pattern": "x"}).startswith("[error]")


def test_grep_invalid_regex_returns_error(tools):
    out = tools["grep_files"].invoke({"pattern": "("})
    assert out.startswith("[error] invalid regex")


def test_read_directory_returns_error_not_raise(tools, tmp_path):
    out = tools["read_file"].invoke({"path": str(tmp_path)})
    assert out.startswith("[error]")


def test_edit_missing_file_returns_error_not_raise(tools):
    out = tools["edit_file"].invoke({"path": "ghost.txt", "old": "a", "new": "b"})
    assert "not found" in out or out.startswith("[error]")


def test_tools_never_raise_with_default_workspace(monkeypatch, tmp_path):
    """The original crash shape: tools with no bound workspace fell back to
    the process cwd. Whatever that cwd is, glob/grep must not raise."""
    monkeypatch.chdir(tmp_path)
    tools = {t.name: t for t in build_builtin_tools(None)}
    assert tools["glob_files"].invoke({"pattern": "**/skills/**"}) == "(no matches)"
    assert tools["grep_files"].invoke({"pattern": "x"}) == "(no matches)"
