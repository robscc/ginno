"""Unit tests for built-in workspace tools (read/write/glob/grep/edit/bash).

Since plan F1 (2026-08) the workspace is BOUND at construction time —
``build_builtin_tools(workspace)`` — and the model never sees a workspace
parameter. These tests drive tools built per workspace; the hardening cases
at the bottom pin the incident behaviour: a bad path yields an ``[error]``
tool result, never an exception that kills the turn.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ginno_runtime.tools.builtin import build_builtin_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def ws(tmp_path):
    # A PROPER subdir of the (test) home — mirrors real session workspaces
    # (projects/<slug>/sessions/<id>/), which the hard-deny rule exempts. A
    # workspace equal to home itself is the workflow cwd fallback and stays
    # protected, so tests must not use home directly.
    d = tmp_path / "ws"
    d.mkdir(exist_ok=True)
    return str(d)


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
    # 'bar' is unique; the result is "ok" + the code-panel change trailer
    # (``<!--ginno-code:-->``, stripped everywhere a human/model reads it —
    # see tests/unit/test_code_changes.py).
    assert tools["edit_file"].invoke(
        {"path": "a.txt", "old": "bar", "new": "BAZ"}
    ).startswith("ok")
    assert tools["read_file"].invoke({"path": "a.txt"}) == "foo BAZ foo"


def test_edit_multiple_matches(tools):
    tools["write_file"].invoke({"path": "a.txt", "content": "foo foo"})
    out = tools["edit_file"].invoke({"path": "a.txt", "old": "foo", "new": "x"})
    assert "multiple matches" in out


def test_edit_not_found(tools):
    tools["write_file"].invoke({"path": "a.txt", "content": "foo"})
    out = tools["edit_file"].invoke({"path": "a.txt", "old": "zzz", "new": "x"})
    assert "not found" in out


def test_concurrent_edits_to_same_file_all_land(tools):
    # 2026-10-08 四联编辑事故回归：langgraph ToolNode 并发执行同一条 AIMessage
    # 里的所有 tool call，同一文件的 4 个 edit_file 曾互相踩踏（lost update +
    # 错位插入 + size=0 撕裂窗口），且各自返回 ok。内置文件工具现在持有进程级
    # 写锁，并发编辑必须逐个完整落盘。
    import threading

    tools["write_file"].invoke(
        {"path": "race.txt", "content": "".join(f"line-{i}-anchor\n" for i in range(1, 5))}
    )

    def run(i: int):
        out = tools["edit_file"].invoke(
            {"path": "race.txt", "old": f"line-{i}-anchor", "new": f"done-{i}"}
        )
        assert out.startswith("ok"), out

    threads = [threading.Thread(target=run, args=(i,)) for i in range(1, 5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    final = tools["read_file"].invoke({"path": "race.txt"})
    for i in range(1, 5):
        assert f"done-{i}" in final
        assert f"line-{i}-anchor" not in final


def test_atomic_edit_leaves_no_tmp_file(tools, ws):
    # _atomic_write_text 的临时文件必须被清理，不污染工作区（glob / 用户可见）。
    tools["write_file"].invoke({"path": "a.txt", "content": "foo"})
    assert tools["edit_file"].invoke({"path": "a.txt", "old": "foo", "new": "bar"}).startswith("ok")
    assert not any(n.endswith(".ginno-tmp") for n in os.listdir(ws))


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


def test_skill_dirs_are_not_home_denied(monkeypatch, tmp_path):
    """~/.ginno/skills (and project skills/) are playbooks, not secrets.
    The model must be able to read SKILL.md and run companion scripts."""
    from ginno_runtime.tools import builtin as tools_builtin

    fake_home = tmp_path / "fake_home"
    ws = fake_home / "projects" / "default" / "sessions" / "s1"
    ws.mkdir(parents=True)
    monkeypatch.setattr(tools_builtin.paths, "home", lambda: fake_home)

    skill = fake_home / "skills" / "aliyun-bill" / "aliyun_bill.py"
    skill.parent.mkdir(parents=True)
    skill.write_text("print('ok')\n", encoding="utf-8")
    (fake_home / "settings.json").write_text("secret", encoding="utf-8")

    tools = {t.name: t for t in tools_builtin.build_builtin_tools(str(ws))}
    assert tools["read_file"].invoke({"path": str(skill)}) == "print('ok')\n"
    assert "denied region" in tools["read_file"].invoke({"path": str(fake_home / "settings.json")})
    bash_out = tools["bash"].invoke({"command": f"python3 {skill}"})
    assert not bash_out.startswith("[error]"), bash_out
    assert "ok" in bash_out


def test_bash_allows_source_user_rc_but_still_denies_secrets(monkeypatch, tmp_path):
    """`source ~/.zshrc` must not be hard-denied — skills load Aliyun/Volc
    keys from the user's rc. ~/.ssh and ~/.ginno/settings stay blocked."""
    from ginno_runtime.tools import builtin as tools_builtin

    fake_home = tmp_path / "fake_home"
    ws = fake_home / "projects" / "default" / "sessions" / "s1"
    ws.mkdir(parents=True)
    monkeypatch.setattr(tools_builtin.paths, "home", lambda: fake_home)

    rc = Path.home() / ".zshrc"
    assert tools_builtin._is_user_rc(Path("~/.zshrc")) is True
    assert tools_builtin._is_user_rc(rc) is True
    assert tools_builtin._is_user_rc(Path.home() / ".ssh" / "id_rsa") is False
    assert tools_builtin._is_user_rc(fake_home / "settings.json") is False

    tools = {t.name: t for t in tools_builtin.build_builtin_tools(str(ws))}
    # Scanner-only: `true` after source short-circuits if the file is
    # missing; we only care that the token is not hard-denied.
    out = tools["bash"].invoke({"command": "source ~/.zshrc; true"})
    assert not out.startswith("[error]"), out

    secret = fake_home / "settings.json"
    secret.write_text("nope", encoding="utf-8")
    denied = tools["bash"].invoke({"command": f"cat {secret}"})
    assert denied.startswith("[error]") and "denied region" in denied

    denied_ssh = tools["bash"].invoke({"command": f"cat {Path.home() / '.ssh' / 'id_rsa'}"})
    assert denied_ssh.startswith("[error]")


# --------------------------------------------------------------------------- #
# 2026-10 perf pass (P1–P4): precomputed deny policy, build-dir pruning,
# read_file pagination, rg delegation. Benchmarks that motivated it: the old
# per-file _path_denied cost ~74µs; a 158k-file walk spent 21s in the check
# against 1.5s of traversal, blowing GLOB_TIME_BUDGET_S by itself.
# --------------------------------------------------------------------------- #
def test_glob_prunes_build_dirs_by_default(tools, ws):
    (Path(ws) / "node_modules" / "pkg").mkdir(parents=True)
    (Path(ws) / "src").mkdir()
    (Path(ws) / "node_modules" / "pkg" / "x.js").write_text("1")
    (Path(ws) / "src" / "app.js").write_text("1")
    out = tools["glob_files"].invoke({"pattern": "**/*.js"})
    assert out.splitlines() == ["src/app.js"]
    out = tools["glob_files"].invoke({"pattern": "**/*.js", "include_ignored": True})
    assert sorted(out.splitlines()) == ["node_modules/pkg/x.js", "src/app.js"]


def test_grep_prunes_build_dirs_by_default(tools, ws):
    (Path(ws) / "node_modules" / "pkg").mkdir(parents=True)
    (Path(ws) / "src").mkdir()
    (Path(ws) / "node_modules" / "pkg" / "x.js").write_text("TOKEN")
    (Path(ws) / "src" / "app.js").write_text("TOKEN")
    out = tools["grep_files"].invoke({"pattern": "TOKEN"})
    assert out.splitlines() == ["src/app.js:1:TOKEN"]
    out = tools["grep_files"].invoke({"pattern": "TOKEN", "include_ignored": True})
    assert sorted(out.splitlines()) == [
        "node_modules/pkg/x.js:1:TOKEN",
        "src/app.js:1:TOKEN",
    ]


def test_grep_skips_binary_oversized_and_symlinked_files(tools, ws):
    w = Path(ws)
    (w / "plain.txt").write_text("TOKEN\n")
    (w / "bin.dat").write_bytes(b"TOKEN\x00binary")
    (w / "huge.txt").write_bytes(b"TOKEN\n" + b"x" * (6 * 1024 * 1024))
    (w / "link_target.txt").write_text("TOKEN\n")
    (w / "link.txt").symlink_to(w / "link_target.txt")
    out = tools["grep_files"].invoke({"pattern": "TOKEN"})
    # The symlink is never followed (rg parity); the plain files match, the
    # binary and oversized ones are skipped.
    assert sorted(out.splitlines()) == ["link_target.txt:1:TOKEN", "plain.txt:1:TOKEN"]


def test_glob_still_lists_allowed_symlinks_but_denies_escaping_ones(tools, ws):
    w = Path(ws)
    (w / "real.txt").write_text("x")
    (w / "ok.txt").symlink_to(w / "real.txt")
    out = tools["glob_files"].invoke({"pattern": "*.txt"})
    assert "ok.txt" in out and "real.txt" in out


def test_read_file_truncates_long_file_with_continue_note(tools, ws):
    (Path(ws) / "big.txt").write_text(
        "\n".join(f"L{i}" for i in range(1, 5001)), encoding="utf-8"
    )
    out = tools["read_file"].invoke({"path": "big.txt"})
    lines = out.splitlines()
    assert (lines[0], lines[1999]) == ("L1", "L2000")
    assert lines[2000] == "... (lines 1-2000 of 5000 — pass offset=2001 to continue)"


def test_read_file_offset_limit_pagination(tools, ws):
    (Path(ws) / "big.txt").write_text(
        "\n".join(f"L{i}" for i in range(1, 5001)), encoding="utf-8"
    )
    out = tools["read_file"].invoke({"path": "big.txt", "offset": 2001, "limit": 10})
    lines = out.splitlines()
    assert (lines[0], lines[9]) == ("L2001", "L2010")
    assert lines[10] == "... (lines 2001-2010 of 5000 — pass offset=2011 to continue)"
    # An explicit limit is honoured but hard-capped: reading past the end of
    # a 5000-line file with limit=99999 yields everything, no note.
    out = tools["read_file"].invoke({"path": "big.txt", "offset": 1, "limit": 99999})
    assert out.splitlines()[-1] == "L5000"


def test_read_file_small_file_is_byte_identical(tools, ws):
    (Path(ws) / "s.txt").write_text("hi\n", encoding="utf-8")
    assert tools["read_file"].invoke({"path": "s.txt"}) == "hi\n"


def test_read_file_offset_beyond_end_is_error(tools, ws):
    (Path(ws) / "s.txt").write_text("hi\n", encoding="utf-8")
    assert tools["read_file"].invoke({"path": "s.txt", "offset": 99}).startswith("[error]")


def test_edit_file_semantics_unchanged_after_find_rewrite(tools, ws):
    (Path(ws) / "e.txt").write_text("aa bb aa", encoding="utf-8")
    assert "multiple matches" in tools["edit_file"].invoke(
        {"path": "e.txt", "old": "aa", "new": "x"}
    )
    assert "not found" in tools["edit_file"].invoke({"path": "e.txt", "old": "zz", "new": "x"})
    assert tools["edit_file"].invoke({"path": "e.txt", "old": "bb", "new": "x"}).startswith("ok")
    assert (Path(ws) / "e.txt").read_text() == "aa x aa"


def test_deny_policy_branches_match_legacy_semantics(monkeypatch, tmp_path):
    from ginno_runtime.tools.builtin import _deny_policy

    fake_home = tmp_path / "fake_home"
    ws = fake_home / "projects" / "default" / "sessions" / "s1"
    ws.mkdir(parents=True)
    (fake_home / "skills").mkdir()
    monkeypatch.setattr("ginno_runtime.tools.builtin.paths.home", lambda: fake_home)

    pol = _deny_policy(ws, [])
    assert pol.denied(fake_home / "settings.json") is True  # home, no exemption
    assert pol.denied(ws / "a.txt") is False  # inside session workspace
    assert pol.denied(fake_home / "skills" / "x" / "SKILL.md") is False  # skill tree
    assert pol.denied(tmp_path / "elsewhere" / "f.py") is False  # outside home
    # A symlink out of the workspace into a denied region is still caught.
    (fake_home / "settings.json").write_text("s", encoding="utf-8")
    leak = ws / "leak"
    leak.symlink_to(fake_home / "settings.json")
    assert pol.denied(leak) is True
    # A project skills/ dir that appears AFTER the policy was built is picked
    # up on the next TTL revalidation.
    (fake_home / "projects" / "p2").mkdir()
    pol._skill_at = 0.0
    assert pol.denied(fake_home / "projects" / "p2" / "skills" / "y") is False


def _write_rg_stub(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "rg-stub"
    stub.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_grep_delegates_to_rg_when_available(tools, ws, monkeypatch, tmp_path):
    """rg on PATH → its JSONL matches are parsed into file:line:text."""
    import json as _json

    w = Path(ws)
    (w / "src").mkdir()
    (w / "src" / "app.js").write_text("TOKEN", encoding="utf-8")
    record = _json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": str(w / "src" / "app.js")},
                "line_number": 3,
                "lines": {"text": "TOKEN here\n"},
            },
        }
    )
    stub = _write_rg_stub(tmp_path, f"print({record!r})\n")
    monkeypatch.setattr("ginno_runtime.tools.builtin.shutil.which", lambda name: str(stub))
    out = tools["grep_files"].invoke({"pattern": "TOKEN"})
    assert out.splitlines() == ["src/app.js:3:TOKEN here"]


def test_grep_rg_hits_are_capped_at_max_hits(tools, ws, monkeypatch, tmp_path):
    import json as _json

    records = "\n".join(
        _json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(Path(ws) / f"f{i}.txt")},
                    "line_number": 1,
                    "lines": {"text": "TOKEN\n"},
                },
            }
        )
        for i in range(60)
    )
    stub = _write_rg_stub(tmp_path, f"print({records!r})\n")
    monkeypatch.setattr("ginno_runtime.tools.builtin.shutil.which", lambda name: str(stub))
    out = tools["grep_files"].invoke({"pattern": "TOKEN"})
    lines = out.splitlines()
    assert len(lines) == 51 and lines[-1] == "... (truncated)"


def test_grep_falls_back_to_python_walk_when_rg_errors(tools, ws, monkeypatch, tmp_path):
    """rg rc=2 (e.g. a Rust-incompatible regex like lookahead) → Python walk
    runs instead, and an invalid pattern still reports [error] invalid regex."""
    (Path(ws) / "app.js").write_text("TOKEN", encoding="utf-8")
    stub = _write_rg_stub(tmp_path, "import sys; sys.exit(2)\n")
    monkeypatch.setattr("ginno_runtime.tools.builtin.shutil.which", lambda name: str(stub))
    out = tools["grep_files"].invoke({"pattern": "TOKEN"})
    assert out.splitlines() == ["app.js:1:TOKEN"]
    # Lookahead compiles in Python but not in Rust regex: rc=2 → fallback
    # still answers from the Python engine instead of failing.
    out = tools["grep_files"].invoke({"pattern": r"TOKE(?=N)"})
    assert out.splitlines() == ["app.js:1:TOKEN"]
    out = tools["grep_files"].invoke({"pattern": "("})
    assert out.startswith("[error] invalid regex")


# ---- 路径读写锁（2026-10-17）：全局锁 → 按规范路径 RWLock（弱引用淘汰）--------
def test_path_rw_lock_identity_and_eviction(tmp_path):
    """持有期间同路径（含别名）同锁、异路径异锁；全部释放后条目蒸发
    （WeakValueDictionary 淘汰，长跑不泄漏）。"""
    import gc

    from ginno_runtime.tools import builtin

    f1 = tmp_path / "a.txt"
    f1_alias = tmp_path / "sub" / ".." / "a.txt"  # realpath 后与 f1 同一文件
    f2 = tmp_path / "b.txt"
    lk1 = builtin._path_rw_lock(f1)
    lk1b = builtin._path_rw_lock(f1_alias)
    lk2 = builtin._path_rw_lock(f2)
    assert lk1 is lk1b
    assert lk1 is not lk2
    n = len(builtin._PATH_RW_LOCKS)
    assert n >= 2
    del lk1, lk1b, lk2
    gc.collect()
    # f2 的锁无持有者 → 蒸发；注册表不再增长泄漏
    assert len(builtin._PATH_RW_LOCKS) < n


def test_rwlock_readers_share_writer_excludes():
    """读共享：两个读者同时在临界区；写独占：读者未清零前写者进不来。"""
    import threading as th

    from ginno_runtime.tools.builtin import _RWLock

    lk = _RWLock()
    lk.acquire_read()
    lk.acquire_read()  # 并发读者 ×2
    got: list[str] = []

    def writer():
        lk.acquire_write()
        got.append("w")
        lk.release_write()

    t = th.Thread(target=writer)
    t.start()
    t.join(0.2)
    assert got == []  # 读者未清零，写者被挡（写优先也会挡住第 3 个读者）
    lk.release_read()
    lk.release_read()
    t.join(2)
    assert got == ["w"]  # 清零后写者进入
    assert not t.is_alive()


def test_write_edit_under_path_locks(tools, ws):
    """功能回归：按路径锁下 write + edit 全程仍原子生效（原有语义保留）。"""
    tools["write_file"].invoke({"path": "x.txt", "content": "hello"})
    r = tools["edit_file"].invoke({"path": "x.txt", "old": "hello", "new": "world"})
    assert r.startswith("ok")
    assert (Path(ws) / "x.txt").read_text() == "world"


def test_read_paginates_large_file_streaming(tools, ws):
    """流式分页：3000 行大文件默认读前 2000 行 + of 3000 提示；offset 续读
    剩余 1000 行且无 suffix——对外行为与整读版逐字节同构。"""
    (Path(ws) / "big.txt").write_text("".join(f"line{i}\n" for i in range(1, 3001)))
    out = tools["read_file"].invoke({"path": "big.txt"})
    lines = out.splitlines()
    assert len(lines) == 2001  # 2000 行 + 1 行 suffix
    assert lines[0] == "line1" and lines[1999] == "line2000"
    assert "of 3000" in lines[2000] and "offset=2001" in lines[2000]
    out2 = tools["read_file"].invoke({"path": "big.txt", "offset": 2001})
    lines2 = out2.splitlines()
    assert len(lines2) == 1000
    assert lines2[0] == "line2001" and lines2[-1] == "line3000"
    assert "offset" not in out2


def test_read_fast_path_empty_and_trailing_newline(tools, ws):
    """快路径逐字节一致：空文件返回空串；行尾 \\n 保留。"""
    (Path(ws) / "empty.txt").write_text("")
    assert tools["read_file"].invoke({"path": "empty.txt"}) == ""
    (Path(ws) / "nl.txt").write_text("a\nb\n")
    assert tools["read_file"].invoke({"path": "nl.txt"}) == "a\nb\n"


# ---- bash 流式采集（2026-10-17）：head+tail 帽 + 超时保留部分输出 --------------
def test_bash_caps_huge_output(tools, ws):
    """5MB stdout 不再全量进内存/context：head+tail 采集，中间省略计数。"""
    out = tools["bash"].invoke(
        {"command": "python3 -c \"print('x' * 5000000)\"", "timeout": 60}
    )
    assert "[exit 0]" in out
    assert "中间省略" in out
    assert len(out) < 60_000  # 远小于 5MB——采集层已封顶


def test_bash_timeout_keeps_partial_output(tools, ws):
    """超时不再只回一行字：已产出的输出保留（旧行为直接全丢）。"""
    out = tools["bash"].invoke({"command": "echo early; sleep 30", "timeout": 2})
    assert "timeout after 2s" in out
    assert "early" in out


# ---- bash：rg 输入类报错在管道下被 exit 0 掩盖 → 追加 [hint] ------------------
# 复刻 2026-10-19 会话 7a9bbb08：backreference 正则 + `| head` → [exit 0] +
# stderr 报错，模型当轮无法察觉。工具须在输出尾部追加显式 [hint]。
def test_rg_usage_hint_classifies_known_errors():
    from ginno_runtime.tools.builtin import _rg_usage_hint

    assert _rg_usage_hint("rg: regex parse error:\n    (?:(a)\\1)\nbackreferences are not supported")
    assert _rg_usage_hint("PCRE2: error matching: match limit exceeded")
    assert _rg_usage_hint("PCRE2 is not available in this build of ripgrep")
    # 非输入类报错不提示
    assert _rg_usage_hint("") is None
    assert _rg_usage_hint("grep: a.txt: No such file or directory") is None


def test_bash_pipeline_masks_rg_error_gets_hint(tools, ws):
    import shutil as _shutil

    rg = _shutil.which("rg")
    if not rg:
        pytest.skip("rg not on PATH")
    tools["write_file"].invoke({"path": "a.txt", "content": "aa bb aa\n"})
    out = tools["bash"].invoke({"command": f"'{rg}' '(a)\\1' a.txt | head -1"})
    assert out.startswith("[exit 0]")  # head 的退出码，掩盖了 rg 的失败
    assert "[hint]" in out
    assert "--pcre2" in out
