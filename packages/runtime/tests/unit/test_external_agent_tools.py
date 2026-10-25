"""Unit tests for external coding-agent delegation (delegate_agent).

Every subprocess/CLI interaction is faked — these tests NEVER invoke a real
external agent (that belongs to the manual verification steps in
docs/external-agents-design.md). Coverage follows the approved plan:
argv construction (2 backends × 2 modes), result parsing, error flattening,
timeout + process-group kill, usage booking (ledger + live accumulator),
the opt-in settings gate, login-shell CLI resolution, and the cwd fallback.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from types import SimpleNamespace

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import server_shared, usage_store
from ginno_runtime.tools import external_agent
from ginno_runtime.tools.external_agent import (
    ClaudeCodeBackend,
    CodexBackend,
    DelegationResult,
    DshBackend,
    _activity_from_line,
    _delegation_header,
    build_external_agent_tools,
    clear_cli_cache,
    run_delegation,
)
from ginno_runtime.tools.external_agents import (
    base as _xa_base,  # adapter 拆分后 _resolve_cli 的真实宿主
)

pytestmark = pytest.mark.unit


CLAUDE_OK = json.dumps(
    {
        "result": "all done",
        "is_error": False,
        "session_id": "sess-abc",
        "num_turns": 8,
        "duration_ms": 142000,
        "cost_usd": 0.03,
        "usage": {
            "input_tokens": 12345,
            "output_tokens": 678,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        },
        "model": "claude-sonnet-5",
    }
)


# dsh headless NDJSON：session → 一轮 → final（含可续跑 sessionId）。
DSH_OK = "\n".join([
    '{"type":"session","sessionId":"session-dsh-test","cwd":"/ws"}',
    '{"type":"status","phase":"turn_start","turn":1}',
    '{"type":"status","phase":"step_start","turn":1,"step":1}',
    '{"type":"text","text":"all done"}',
    '{"type":"status","phase":"step_end","turn":1,"step":1,"usage":{"inputTokens":100,"outputTokens":10,"cacheReadTokens":20}}',
    '{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"completed"}}',
    '{"type":"final","text":"all done"}',
])

# 续跑流：同 sessionId、turn 从 2 起。
DSH_RESUMED = "\n".join([
    '{"type":"session","sessionId":"session-dsh-test","cwd":"/ws"}',
    '{"type":"status","phase":"turn_start","turn":2}',
    '{"type":"status","phase":"step_start","turn":2,"step":1}',
    '{"type":"status","phase":"turn_end","turn":2,"reason":{"kind":"completed"}}',
    '{"type":"final","text":"42"}',
])


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _cli_cache_clean():
    clear_cli_cache()
    yield
    clear_cli_cache()


def _stub_cli(monkeypatch, path):
    """CLI 桩：直接写 base._CLI_CACHE。adapter 拆分后各 backend 在导入期
    绑定了 _resolve_cli 引用，patch 模块属性够不到它们；cache 是共享
    dict（运行期读），一处注入对全部 adapter 生效。autouse 的
    _cli_cache_clean 负责清理。

    版本缓存一并预填（版本=各后端最小版本门）——cli_version 否则会真起
    ``--version`` 子进程，被本文件 monkeypatch 的 Popen 截胡后污染
    fake_popen 的 created[0]（2026-10-18 A2 引入探针后实测）。"""
    for _name in ("claude", "codex", "pi", "dsh"):
        _xa_base._CLI_CACHE[_name] = path
        _xa_base._CLI_VERSION_CACHE[_name] = (
            path,
            _xa_base._MIN_CLI_VERSIONS.get(_name),
        )


@pytest.fixture
def ws(tmp_path):
    # A real subdir (mirrors session workspaces; keeps parity with the
    # builtin-tools tests' home-deny conventions).
    d = tmp_path / "ws"
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("GINNO_EXTERNAL_AGENTS", "1")


@pytest.fixture
def delegate(ws, enabled):
    # 异步化后（2026-10-04）：unit 层只测无会话的同步路径；带会话的异步
    # 注入/记账由 tests/api/test_delegate_wiring.py 覆盖。
    tools = build_external_agent_tools(ws)
    assert len(tools) == 1 and tools[0].name == "delegate_agent"
    return tools[0]


def fake_popen(monkeypatch, *, stdout="", stderr="", rc=0, timeout_once=False):
    """Patch subprocess.Popen with a recorder; returns the created instances."""
    created = []

    class _P:
        def __init__(self, argv, **kw):
            self.argv = list(argv)
            self.kw = kw
            self.pid = 4242
            self.returncode = rc
            self.timeouts = []
            self._comm = 0
            created.append(self)

        def communicate(self, timeout=None):
            self._comm += 1
            self.timeouts.append(timeout)
            if timeout_once and self._comm == 1:
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
            return stdout, stderr

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", _P)
    return created


# --------------------------------------------------------------------------- #
# argv construction (2 backends × 2 modes)
# --------------------------------------------------------------------------- #
def test_claude_argv_read_only(monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    argv = ClaudeCodeBackend().build_argv("do it", "read-only", "/ws", None)
    assert argv[0] == "/opt/claude"
    assert argv[1] == "-p"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    # 2026-10-05 用户决策：完整工具表 + --dangerously-skip-permissions
    # （headless 无人应答权限提示），permissions.deny/--settings 已移除。
    assert "--dangerously-skip-permissions" in argv
    assert not any(a.startswith("--settings=") for a in argv)
    assert argv[-1] == "do it"


def test_claude_argv_edit(monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    argv = ClaudeCodeBackend().build_argv("do it", "edit", "/ws", None)
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode=acceptEdits" in argv
    assert "plan" not in argv


def test_codex_argv_read_only(monkeypatch):
    _stub_cli(monkeypatch, "/opt/codex")
    argv = CodexBackend().build_argv("do it", "read-only", "/ws", "/tmp/out.txt")
    assert argv[0] == "/opt/codex"
    assert argv[1] == "exec"
    assert argv[argv.index("-C") + 1] == "/ws"
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("--output-last-message") + 1] == "/tmp/out.txt"
    assert "--full-auto" not in argv
    assert argv[-1] == "do it"


def test_codex_argv_edit(monkeypatch):
    _stub_cli(monkeypatch, "/opt/codex")
    argv = CodexBackend().build_argv("do it", "edit", "/ws", None)
    assert "--full-auto" in argv
    assert "-s" not in argv


def test_prompt_stays_a_single_argv_element(monkeypatch):
    """Prompt injection into argv: shell metacharacters must remain one
    element (we never go through a shell)."""
    _stub_cli(monkeypatch, "/opt/claude")
    evil = "a b; rm -rf / $(reboot) `x`"
    argv = ClaudeCodeBackend().build_argv(evil, "read-only", "/ws", None)
    assert argv[-1] == evil


# --------------------------------------------------------------------------- #
# claude JSON parsing
# --------------------------------------------------------------------------- #
def test_claude_parse_success():
    res = ClaudeCodeBackend().parse_result(CLAUDE_OK, "", 0, None)
    assert res.stop_reason == "success"
    assert res.output == "all done"
    # Ginno normalization: input_tokens is the WHOLE prompt (cache included).
    assert res.usage["input_tokens"] == 12345 + 100 + 50
    assert res.usage["output_tokens"] == 678
    assert res.usage["cache_read_tokens"] == 100
    assert res.meta["session_id"] == "sess-abc"
    assert res.meta["num_turns"] == 8
    assert res.meta["duration_ms"] == 142000


def test_claude_parse_is_error_flag():
    data = json.loads(CLAUDE_OK)
    data["is_error"] = True
    res = ClaudeCodeBackend().parse_result(json.dumps(data), "bad stuff", 1, None)
    assert res.stop_reason == "error"
    assert "bad stuff" in res.diagnostic
    assert res.usage  # usage still parsed for bookkeeping


def test_claude_parse_json_with_surrounding_noise():
    res = ClaudeCodeBackend().parse_result(f"banner line\n{CLAUDE_OK}\ntrailer", "", 0, None)
    assert res.stop_reason == "success"
    assert res.output == "all done"


def test_claude_parse_not_json():
    res = ClaudeCodeBackend().parse_result("hello world", "boom " * 900, 2, None)
    assert res.stop_reason == "error"
    assert len(res.diagnostic) <= 2001  # _tail cap (+ the leading ellipsis)


# --------------------------------------------------------------------------- #
# codex parsing
# --------------------------------------------------------------------------- #
def test_codex_parse_reads_last_message_file(tmp_path):
    f = tmp_path / "last.txt"
    f.write_text("final answer")
    res = CodexBackend().parse_result("progress noise", "", 0, str(f))
    assert res.stop_reason == "success"
    assert res.output == "final answer"


def test_codex_parse_missing_last_message_falls_back_to_stdout():
    res = CodexBackend().parse_result("stream text", "", 0, None)
    assert res.stop_reason == "success"
    # The degradation note travels IN the output — diagnostics are only
    # surfaced on failure, but the model must see this is a stream tail.
    assert res.output.startswith("[note]")
    assert "stream text" in res.output
    assert res.diagnostic  # bookkeeping for logs/tests


def test_codex_parse_rc_nonzero():
    res = CodexBackend().parse_result("x", "err " * 1000, 1, None)
    assert res.stop_reason == "error"
    assert len(res.diagnostic) <= 2001


# --------------------------------------------------------------------------- #
# Tool-level validation errors (all flattened, never raise)
# --------------------------------------------------------------------------- #
def test_unknown_backend(delegate):
    out = delegate.invoke({"backend": "openai", "prompt": "x"})
    assert "[error] unknown backend" in out
    assert "claude-code" in out and "codex" in out


def test_empty_prompt(delegate):
    out = delegate.invoke({"backend": "claude-code", "prompt": "   "})
    assert out.startswith("[error]")


def test_unknown_mode(delegate, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    out = delegate.invoke({"backend": "claude-code", "prompt": "x", "mode": "yolo"})
    assert "[error] unknown mode" in out


def test_backend_not_installed_lists_available(delegate, monkeypatch):
    _stub_cli(monkeypatch, None)
    out = delegate.invoke({"backend": "codex", "prompt": "x"})
    assert "[error] codex is not installed" in out


def test_description_lists_availability(delegate):
    desc = delegate.description or ""
    assert (
        "Available backends on this machine:" in desc
        or "No external agent CLI is installed" in desc
    )


# --------------------------------------------------------------------------- #
# End-to-end tool runs (fake subprocess)
# --------------------------------------------------------------------------- #
def test_delegate_success_header_and_cwd(delegate, ws, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    out = delegate.invoke({"backend": "claude-code", "prompt": "do x"})
    assert out.startswith("[delegate backend=claude-code mode=read-only stop=success")
    assert "turns=8" in out
    assert "duration=142s" in out
    assert "tokens=12495/678" in out
    assert "all done" in out
    p = created[0]
    assert p.kw["cwd"] == ws
    assert p.kw["start_new_session"] is (os.name == "posix")


def test_default_mode_is_read_only(delegate, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    delegate.invoke({"backend": "claude-code", "prompt": "x"})
    argv = created[0].argv
    assert not any(a.startswith("--permission-mode") for a in argv)


def test_error_rc_flattens_to_diagnostic(delegate, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    fake_popen(monkeypatch, stdout="not json", stderr="auth failed", rc=1)
    out = delegate.invoke({"backend": "claude-code", "prompt": "x"})
    assert "stop=error" in out
    assert "[diagnostic]" in out
    assert "auth failed" in out


def test_timeout_kills_process_group_and_reports(delegate, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, timeout_once=True)
    signals = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    out = delegate.invoke({"backend": "claude-code", "prompt": "x", "timeout": 999999})
    assert "stop=timeout after 1800s" in out  # clamped to the max
    assert "[diagnostic]" in out
    assert created[0].timeouts[0] == 1800
    assert signals and signals[0] == (4242, signal.SIGTERM)


def test_timeout_clamps_up_to_minimum(delegate, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    delegate.invoke({"backend": "claude-code", "prompt": "x", "timeout": 1})
    assert created[0].timeouts[0] == 10  # clamped to the min


def test_workspace_none_falls_back_to_process_cwd(monkeypatch, enabled):
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    tools = build_external_agent_tools(None)
    tools[0].invoke({"backend": "claude-code", "prompt": "x"})
    assert created[0].kw["cwd"] == os.getcwd()


# --------------------------------------------------------------------------- #
# Usage booking (ledger + live accumulator)
# --------------------------------------------------------------------------- #
def test_usage_recorded_and_accumulator_synced(monkeypatch):
    """账本 + 实时累加器双写（seam 不变量）；带会话的端到端记账在
    tests/api/test_delegate_wiring.py 的异步注入路径覆盖。"""
    recorded = []
    monkeypatch.setattr(usage_store, "record", lambda **kw: recorded.append(kw))
    server_shared._USAGE_BY_SESSION.pop("sess-1", None)
    from ginno_runtime.tools.external_agent import _record_external_usage

    _record_external_usage(
        {"input_tokens": 12495, "output_tokens": 678},
        "claude-code",
        {"model": "claude-sonnet-5"},
        "sess-1",
        "proj-x",
    )

    assert recorded
    r = recorded[0]
    assert r["source"] == "external"
    assert r["provider"] == "claude-code"
    assert r["model"] == "claude-sonnet-5"
    assert r["session_id"] == "sess-1"
    assert r["project_slug"] == "proj-x"
    assert r["input_tokens"] == 12495
    assert r["output_tokens"] == 678

    acc = server_shared._USAGE_BY_SESSION.get("sess-1")
    assert acc and acc["input_tokens"] == 12495 and acc["calls"] == 1


def test_no_usage_booking_without_session(ws, enabled, monkeypatch):
    """Workflow/headless path: no session attribution → no ledger writes."""
    recorded = []
    monkeypatch.setattr(usage_store, "record", lambda **kw: recorded.append(kw))
    _stub_cli(monkeypatch, "/opt/claude")
    fake_popen(monkeypatch, stdout=CLAUDE_OK)
    tools = build_external_agent_tools(ws)
    out = tools[0].invoke({"backend": "claude-code", "prompt": "x"})
    assert out.startswith("[delegate")
    assert not recorded


# --------------------------------------------------------------------------- #
# Settings gate (default OFF, env override)
# --------------------------------------------------------------------------- #
def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("GINNO_EXTERNAL_AGENTS", raising=False)
    assert build_external_agent_tools("/tmp") == []


def test_gate_enabled_via_settings(monkeypatch):
    from ginno_runtime import paths

    monkeypatch.delenv("GINNO_EXTERNAL_AGENTS", raising=False)
    paths.settings_path().write_text(
        json.dumps({"context": {"external_agents_enabled": True}})
    )
    tools = build_external_agent_tools("/tmp")
    assert [t.name for t in tools] == ["delegate_agent"]


def test_env_override_beats_settings(monkeypatch):
    from ginno_runtime import paths

    paths.settings_path().write_text(
        json.dumps({"context": {"external_agents_enabled": False}})
    )
    monkeypatch.setenv("GINNO_EXTERNAL_AGENTS", "1")
    assert len(build_external_agent_tools("/tmp")) == 1
    monkeypatch.setenv("GINNO_EXTERNAL_AGENTS", "off")
    assert build_external_agent_tools("/tmp") == []


# --------------------------------------------------------------------------- #
# CLI resolution (GUI PATH caveat — login-shell fallback)
# --------------------------------------------------------------------------- #
def test_resolve_cli_prefers_shutil_which(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda n: "/opt/bin/claude")
    assert external_agent._resolve_cli("claude") == "/opt/bin/claude"


def test_resolve_cli_falls_back_to_login_shell(monkeypatch):
    # which 桩须吃下第二次调用的 path= kwarg（2026-10-06 的显式目录补充）
    monkeypatch.setattr(shutil, "which", lambda n, path=None: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="/opt/local/bin/claude\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert external_agent._resolve_cli("claude") == "/opt/local/bin/claude"
    assert seen["argv"][:2] == ["/bin/zsh", "-lc"]
    assert "command -v claude" in seen["argv"][2]


def test_resolve_cli_nowhere(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda n, path=None: None)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    )
    assert external_agent._resolve_cli("codex") is None


def test_run_delegation_streaming_on_line(monkeypatch):
    """流式路径：真实子进程逐行回调 on_line（fake 无 stdout 时自动落回
    communicate——本测试用 /bin/sh printf 模拟 JSONL 逐行输出）。"""
    import io as _io
    import threading as _th

    class _StreamP:
        def __init__(self, argv, **kw):
            self.argv = list(argv)
            self.pid = 4242
            self.returncode = 0
            self.stdout = _io.StringIO('{"type":"result","subtype":"success","result":"ok","is_error":false}\n')
            self.stderr = _io.StringIO("")

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    seen = []
    monkeypatch.setattr(subprocess, "Popen", _StreamP)
    _stub_cli(monkeypatch, "/opt/claude")
    res = run_delegation(
        ClaudeCodeBackend(), "p", "read-only", "/ws", 30, on_line=seen.append
    )
    assert res.stop_reason == "success" and res.output == "ok"
    assert seen and seen[0].startswith("{")  # 逐行收到


# --------------------------------------------------------------------------- #
# A2（2026-10-18）：ENOENT 缓存自愈 + 版本探针/最小版本门
# --------------------------------------------------------------------------- #
def test_run_delegation_enoent_self_heals(monkeypatch):
    """pinned 路径消失（ENOENT）→ 踢缓存重解析；新路径进入重试的 argv。"""
    calls = {"n": 0, "invalidated": []}

    class _P:
        def __init__(self, argv, **kw):
            calls["n"] += 1
            calls["argv"] = list(argv)
            if calls["n"] == 1:
                raise FileNotFoundError(2, "No such file", argv[0])
            self.pid = 4242
            self.returncode = 0
            self.stdout = None
            self.stderr = None

        def communicate(self, timeout=None):
            return CLAUDE_OK, ""

    monkeypatch.setattr(subprocess, "Popen", _P)
    _stub_cli(monkeypatch, "/gone/claude")
    monkeypatch.setattr(
        external_agent, "invalidate_cli",
        lambda name: calls["invalidated"].append(name) or _xa_base._CLI_CACHE.update({"claude": "/new/claude"}),
    )
    res = run_delegation(ClaudeCodeBackend(), "p", "read-only", "/ws", 30)
    assert res.stop_reason == "success"
    assert calls["n"] == 2
    assert calls["invalidated"] == ["claude"]
    assert calls["argv"][0] == "/new/claude"  # 重拼 argv 用了新路径


def test_run_delegation_enoent_twice_is_error(monkeypatch):
    """重解析后仍 ENOENT → 报错退出，不无限重试。"""

    class _P:
        def __init__(self, argv, **kw):
            raise FileNotFoundError(2, "No such file", argv[0])

    monkeypatch.setattr(subprocess, "Popen", _P)
    _stub_cli(monkeypatch, "/gone/claude")
    monkeypatch.setattr(external_agent, "invalidate_cli", lambda name: None)
    res = run_delegation(ClaudeCodeBackend(), "p", "read-only", "/ws", 30)
    assert res.stop_reason == "error" and "cannot launch" in res.diagnostic


def test_cli_version_parse_and_cache(monkeypatch):
    calls = []

    def _fake_run(*a, **kw):
        calls.append(a)
        return SimpleNamespace(returncode=0, stdout="2.1.229 (Claude Code)", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _stub_cli(monkeypatch, "/opt/claude")
    # _stub_cli 预填了版本缓存（防探针截胡 fake_popen）——本测试要测探针
    # 本身,先踢掉 claude 的版本条目。
    _xa_base._CLI_VERSION_CACHE.pop("claude", None)
    v = _xa_base.cli_version("claude")
    assert v == (2, 1, 229)
    assert calls  # 探针确实跑了一次 --version
    # 缓存：同路径第二次不再起进程
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached")),
    )
    assert _xa_base.cli_version("claude") == (2, 1, 229)


def test_cli_version_warning_gate(monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    monkeypatch.setattr(_xa_base, "_CLI_VERSION_CACHE", {"claude": ("/opt/claude", (1, 9, 9))})
    w = _xa_base.cli_version_warning("claude")
    assert "v1.9.9" in w and "below the recommended minimum v2.0.0" in w
    # 达标 / 无门 / 解析不出 → 无告警
    monkeypatch.setattr(_xa_base, "_CLI_VERSION_CACHE", {"claude": ("/opt/claude", (2, 0, 0))})
    assert _xa_base.cli_version_warning("claude") == ""
    monkeypatch.setattr(_xa_base, "_CLI_VERSION_CACHE", {"pi": ("/opt/pi", None)})
    assert _xa_base.cli_version_warning("pi") == ""


def test_delegate_tool_surfaces_version_warning(monkeypatch, delegate):
    _stub_cli(monkeypatch, "/opt/claude")
    monkeypatch.setattr(
        external_agent, "cli_version_warning",
        lambda name: "[warning] too old" if name == "claude" else "",
    )
    fake_popen(monkeypatch, stdout=CLAUDE_OK)
    out = delegate.invoke({"backend": "claude-code", "prompt": "p"})
    assert out.startswith("[warning] too old\n")
    assert "all done" in out


# --------------------------------------------------------------------------- #
# A3（2026-10-18）：timeout=0 的空闲看门狗
# --------------------------------------------------------------------------- #
def test_idle_watchdog_kills_silent_child(monkeypatch):
    """timeout=0 时失声超过 idle 阈值 → 杀整树 + stop=timeout(idle)。"""
    import io as _io
    import threading as _th

    hang = _th.Event()
    signals = []

    def _gen():
        yield '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}\n'
        hang.wait(10)  # 模拟失声挂死

    class _HangP:
        def __init__(self, argv, **kw):
            self.pid = 4242
            self.returncode = 0
            self.stdout = _gen()
            self.stderr = _io.StringIO("")

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _HangP)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(external_agent, "_IDLE_WATCHDOG_S", 1)
    _stub_cli(monkeypatch, "/opt/claude")
    res = run_delegation(ClaudeCodeBackend(), "p", "read-only", "/ws", 0)
    hang.set()
    assert res.stop_reason == "timeout"
    assert res.meta["terminal_reason"] == "idle_timeout"
    assert "idle" in res.diagnostic
    assert signals and signals[0] == (4242, signal.SIGTERM)


def test_wall_timeout_still_applies_when_nonzero(monkeypatch):
    """timeout>0 时墙钟照旧（不因 idle 看门狗而弱化）。"""
    import io as _io
    import threading as _th

    hang = _th.Event()

    def _gen():
        # 真·生成器：迭代即阻塞（无 yield 会退化成普通函数,stdout=None
        # 落回 communicate 路径,测不到流式监督）。
        yield from ()
        hang.wait(10)

    class _HangP:
        def __init__(self, argv, **kw):
            self.pid = 4242
            self.returncode = 0
            self.stdout = _gen()
            self.stderr = _io.StringIO("")

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _HangP)
    _stub_cli(monkeypatch, "/opt/claude")
    res = run_delegation(ClaudeCodeBackend(), "p", "read-only", "/ws", 1)
    hang.set()
    assert res.stop_reason == "timeout"
    assert res.meta["terminal_reason"] == "wall_timeout"


# --------------------------------------------------------------------------- #
# dsh：注册 / build_env 合并 / 续跑 session_id 透传（2026-10-25）
# --------------------------------------------------------------------------- #
def test_backend_registry_includes_dsh():
    assert set(external_agent._BACKENDS) == {"claude-code", "codex", "pi", "dsh"}
    assert external_agent._BACKENDS["dsh"].supports_resume is True
    # 其余后端不宣称支持续跑
    assert not getattr(external_agent._BACKENDS["claude-code"], "supports_resume", False)


def test_dsh_build_env_merged_into_popen(delegate, monkeypatch):
    """dsh 的权限模式走 DSH_PERMISSION_MODE：run_delegation 把它合并进
    os.environ 后交给 Popen（父进程环境保留）。"""
    _stub_cli(monkeypatch, "/opt/dsh")
    created = fake_popen(monkeypatch, stdout=DSH_OK)
    out = delegate.invoke(
        {"backend": "dsh", "prompt": "do x", "session_id": "session-abc"}
    )
    p = created[0]
    assert p.argv[0] == "/opt/dsh"
    assert p.argv[:4] == ["/opt/dsh", "--profile", "headless", "--json"]
    # 续跑 id 在 prompt 之前
    assert p.argv.index("--session-id") + 1 < p.argv.index("do x")
    assert p.argv[p.argv.index("--session-id") + 1] == "session-abc"
    assert p.kw["env"]["DSH_PERMISSION_MODE"] == "read-only"
    assert p.kw["env"]["PATH"] == os.environ["PATH"]  # 继承父进程环境
    # 机器头暴露可续跑 id + 本次调用轮数（turn 数是本次=1，不是会话级）
    assert "stop=success session=session-dsh-test turns=1" in out
    assert "all done" in out

    # edit → workspace-write（argv 不变，只有 env 变）
    created = fake_popen(monkeypatch, stdout=DSH_RESUMED)
    out2 = delegate.invoke({"backend": "dsh", "prompt": "next", "mode": "edit"})
    assert created[0].kw["env"]["DSH_PERMISSION_MODE"] == "workspace-write"
    assert "--session-id" not in created[0].argv
    # 续跑流 turn=2，但本工具头只反映本次调用轮数 → turns=1
    assert "session=session-dsh-test" in out2 and "turns=1" in out2


def test_backend_without_build_env_gets_no_env_kw(delegate, monkeypatch):
    """没有 build_env 的后端行为必须不变：Popen 不传 env（继承环境）。"""
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    delegate.invoke({"backend": "claude-code", "prompt": "x"})
    assert "env" not in created[0].kw


def test_run_delegation_tolerates_legacy_build_argv(monkeypatch):
    """session_id 透传对旧式（4 参 build_argv）fake 后端仍向后兼容。"""

    class _Legacy:
        name = "legacy"
        cli_name = ""

        def build_argv(self, prompt, mode, cwd, out_file):
            return ["legacy-cli", prompt]

        def parse_result(self, stdout, stderr, rc, out_file):
            return DelegationResult(output="legacy ok")

    created = fake_popen(monkeypatch, stdout="")
    res = run_delegation(
        _Legacy(), "p", "read-only", "/ws", 30, session_id="session-abc"
    )
    assert res.output == "legacy ok" and res.stop_reason == "success"
    assert created[0].argv == ["legacy-cli", "p"]


async def test_delegation_bg_passes_resume_session_id(monkeypatch):
    """后台路径同样把续跑 id 传给 run_delegation（不只是同步路径）。"""
    calls = {}

    def _fake_run(backend, prompt, mode, cwd, timeout, on_line=None,
                  proc_cb=None, session_id=None):
        calls["session_id"] = session_id
        return DelegationResult(output="ok")

    monkeypatch.setattr(external_agent, "run_delegation", _fake_run)
    # delegation_id="" + session_id="" → 不落库、不注入、不记账，只测透传
    await external_agent._delegation_bg(
        "", "", "default", DshBackend(), "dsh", "p", "read-only", "/ws", 30,
        resume_session_id="session-abc",
    )
    assert calls["session_id"] == "session-abc"


def test_session_id_rejected_for_backends_without_resume(delegate, monkeypatch):
    _stub_cli(monkeypatch, "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    out = delegate.invoke(
        {"backend": "claude-code", "prompt": "x", "session_id": "session-abc"}
    )
    assert out == "[error] backend claude-code does not support session continuation"
    assert created == []  # 绝不启动进程


def test_delegation_header_session_only_for_resumable_backends():
    """机器头：可续跑后端且解析到 session_id 才加 session=，位置在 stop= 后、
    turns= 前；不支持续跑的后端/无 session 时不出现。"""
    from ginno_runtime.tools.external_agents.base import DelegationResult

    h = _delegation_header(
        "dsh", "read-only", 60,
        DelegationResult(meta={"session_id": "session-abc", "num_turns": 1}),
    )
    assert h == "[delegate backend=dsh mode=read-only stop=success session=session-abc turns=1]"
    h2 = _delegation_header(
        "dsh", "read-only", 60, DelegationResult(meta={"num_turns": 1})
    )
    assert "session=" not in h2
    h3 = _delegation_header(
        "claude-code", "read-only", 60,
        DelegationResult(meta={"session_id": "sess-abc", "num_turns": 1}),
    )
    assert "session=" not in h3
    # session id 里的空白压成单个空格（机器头是单行 kv）
    h4 = _delegation_header(
        "dsh", "read-only", 60, DelegationResult(meta={"session_id": " a  b "})
    )
    assert " session=a b" in h4


def test_activity_from_line_dsh_events():
    assert _activity_from_line('{"type":"tool_call","tool":"bash"}') == "⚙ bash"
    assert _activity_from_line('{"type":"text","text":"hello"}') == "hello"
    # thinking/status 不产生活动文案；非 JSON 行静默
    assert _activity_from_line('{"type":"thinking","text":"x"}') == ""
    assert _activity_from_line('{"type":"status","phase":"step_start"}') == ""
    assert _activity_from_line("noise line") == ""
    assert _activity_from_line('{"type":"text","text":""}') == ""
