"""Unit tests for built-in workspace tools (read/write/glob/grep/edit/bash)."""

from __future__ import annotations

import pytest

from ginno_runtime.tools.builtin import (
    bash,
    build_builtin_tools,
    edit_file,
    glob_files,
    grep_files,
    read_file,
    write_file,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def ws(tmp_path):
    return str(tmp_path)


def test_write_then_read_roundtrip(ws):
    write_file.invoke({"path": "a.txt", "content": "hello", "workspace": ws})
    assert read_file.invoke({"path": "a.txt", "workspace": ws}) == "hello"


def test_read_missing_file(ws):
    out = read_file.invoke({"path": "missing.txt", "workspace": ws})
    assert "[error] file not found" in out


def test_write_creates_nested_dirs(ws):
    write_file.invoke({"path": "sub/dir/a.txt", "content": "x", "workspace": ws})
    assert read_file.invoke({"path": "sub/dir/a.txt", "workspace": ws}) == "x"


def test_glob_files(ws):
    write_file.invoke({"path": "one.md", "content": "1", "workspace": ws})
    write_file.invoke({"path": "two.md", "content": "2", "workspace": ws})
    write_file.invoke({"path": "three.txt", "content": "3", "workspace": ws})
    out = glob_files.invoke({"pattern": "*.md", "workspace": ws})
    assert "one.md" in out and "two.md" in out
    assert "three.txt" not in out


def test_grep_files(ws):
    write_file.invoke({"path": "code.py", "content": "def foo():\n    return 42\n", "workspace": ws})
    out = grep_files.invoke({"pattern": "return 42", "workspace": ws})
    assert "code.py:2:" in out


def test_grep_no_match(ws):
    write_file.invoke({"path": "code.py", "content": "x = 1\n", "workspace": ws})
    assert grep_files.invoke({"pattern": "zzz", "workspace": ws}) == "(no matches)"


def test_edit_unique_match(ws):
    write_file.invoke({"path": "a.txt", "content": "foo bar foo", "workspace": ws})
    # 'bar' is unique
    assert edit_file.invoke({"path": "a.txt", "old": "bar", "new": "BAZ", "workspace": ws}) == "ok"
    assert read_file.invoke({"path": "a.txt", "workspace": ws}) == "foo BAZ foo"


def test_edit_multiple_matches(ws):
    write_file.invoke({"path": "a.txt", "content": "foo foo", "workspace": ws})
    out = edit_file.invoke({"path": "a.txt", "old": "foo", "new": "x", "workspace": ws})
    assert "multiple matches" in out


def test_edit_not_found(ws):
    write_file.invoke({"path": "a.txt", "content": "foo", "workspace": ws})
    out = edit_file.invoke({"path": "a.txt", "old": "zzz", "new": "x", "workspace": ws})
    assert "not found" in out


def test_bash_captures_exit_and_output(ws):
    out = bash.invoke({"command": "echo hi; exit 3", "workspace": ws})
    assert "[exit 3]" in out
    assert "hi" in out


def test_bash_timeout(ws):
    out = bash.invoke({"command": "sleep 5", "workspace": ws, "timeout": 1})
    assert "timeout" in out


def test_build_builtin_tools_returns_six():
    names = {t.name for t in build_builtin_tools()}
    assert names == {"read_file", "write_file", "glob_files", "grep_files", "edit_file", "bash"}
