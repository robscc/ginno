"""Unit tests for external coding-agent delegation (delegate_agent).

Every subprocess/CLI interaction is faked — these tests NEVER invoke a real
external agent (that belongs to the manual verification steps in
docs/external-agents-design.md). Coverage follows the approved plan:
argv construction (2 backends × 2 modes), result parsing, error flattening,
timeout + process-group kill, usage booking (ledger + live accumulator),
the opt-in settings gate, login-shell CLI resolution, and the cwd fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from types import SimpleNamespace

import pytest

from ginno_runtime import server_shared, usage_store
from ginno_runtime.tools import external_agent
from ginno_runtime.tools.external_agent import (
    ClaudeCodeBackend,
    CodexBackend,
    build_external_agent_tools,
    clear_cli_cache,
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


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _cli_cache_clean():
    clear_cli_cache()
    yield
    clear_cli_cache()


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
    tools = build_external_agent_tools(ws, session_id="sess-1", project_slug="proj-x")
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
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    argv = ClaudeCodeBackend().build_argv("do it", "read-only", "/ws", None)
    assert argv[0] == "/opt/claude"
    assert argv[1] == "-p"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--restricted" in argv  # BOTH modes — strips Bash entirely
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[-1] == "do it"


def test_claude_argv_edit(monkeypatch):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    argv = ClaudeCodeBackend().build_argv("do it", "edit", "/ws", None)
    assert "--restricted" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "plan" not in argv


def test_codex_argv_read_only(monkeypatch):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/codex")
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
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/codex")
    argv = CodexBackend().build_argv("do it", "edit", "/ws", None)
    assert "--full-auto" in argv
    assert "-s" not in argv


def test_prompt_stays_a_single_argv_element(monkeypatch):
    """Prompt injection into argv: shell metacharacters must remain one
    element (we never go through a shell)."""
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
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
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    out = delegate.invoke({"backend": "claude-code", "prompt": "x", "mode": "yolo"})
    assert "[error] unknown mode" in out


def test_backend_not_installed_lists_available(delegate, monkeypatch):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: None)
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
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
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
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    delegate.invoke({"backend": "claude-code", "prompt": "x"})
    argv = created[0].argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_error_rc_flattens_to_diagnostic(delegate, monkeypatch):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    fake_popen(monkeypatch, stdout="not json", stderr="auth failed", rc=1)
    out = delegate.invoke({"backend": "claude-code", "prompt": "x"})
    assert "stop=error" in out
    assert "[diagnostic]" in out
    assert "auth failed" in out


def test_timeout_kills_process_group_and_reports(delegate, monkeypatch):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    created = fake_popen(monkeypatch, timeout_once=True)
    signals = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    out = delegate.invoke({"backend": "claude-code", "prompt": "x", "timeout": 999999})
    assert "stop=timeout after 1800s" in out  # clamped to the max
    assert "[diagnostic]" in out
    assert created[0].timeouts[0] == 1800
    assert signals and signals[0] == (4242, signal.SIGTERM)


def test_timeout_clamps_up_to_minimum(delegate, monkeypatch):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    delegate.invoke({"backend": "claude-code", "prompt": "x", "timeout": 1})
    assert created[0].timeouts[0] == 10  # clamped to the min


def test_workspace_none_falls_back_to_process_cwd(monkeypatch, enabled):
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    created = fake_popen(monkeypatch, stdout=CLAUDE_OK)
    tools = build_external_agent_tools(None, session_id="s")
    tools[0].invoke({"backend": "claude-code", "prompt": "x"})
    assert created[0].kw["cwd"] == os.getcwd()


# --------------------------------------------------------------------------- #
# Usage booking (ledger + live accumulator)
# --------------------------------------------------------------------------- #
def test_usage_recorded_and_accumulator_synced(delegate, monkeypatch):
    recorded = []
    monkeypatch.setattr(usage_store, "record", lambda **kw: recorded.append(kw))
    server_shared._USAGE_BY_SESSION.pop("sess-1", None)
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
    fake_popen(monkeypatch, stdout=CLAUDE_OK)
    delegate.invoke({"backend": "claude-code", "prompt": "x"})

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
    monkeypatch.setattr(external_agent, "_resolve_cli", lambda n: "/opt/claude")
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
    monkeypatch.setattr(shutil, "which", lambda n: None)
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
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    )
    assert external_agent._resolve_cli("codex") is None
