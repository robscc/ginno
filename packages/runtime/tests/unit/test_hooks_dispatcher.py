"""Unit tests for the hook dispatcher: matcher filtering and subprocess effects."""

from __future__ import annotations

import sys

import pytest

from ginno_runtime.hooks.dispatcher import HookDispatcher, HookEvent

pytestmark = pytest.mark.unit


def _dispatcher(settings):
    return HookDispatcher(settings=settings)


def test_matcher_filtering(isolated_home):
    d = _dispatcher(
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "command": "echo x"},
                    {"command": "echo y"},  # no matcher -> applies to all
                ]
            }
        }
    )
    # Bash matches both the specific and the matcher-less hook
    assert len(d._hooks_for("PreToolUse", "Bash")) == 2
    # Write matches only the matcher-less hook
    assert len(d._hooks_for("PreToolUse", "Write")) == 1
    # matcher-less event (no matcher arg) returns only matcher-less hooks
    assert len(d._hooks_for("UserPromptSubmit", None)) == 0


async def test_dispatch_block(isolated_home, tmp_path):
    hook = tmp_path / "block.py"
    hook.write_text(
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'block': True, 'reason': 'nope'}))\n"
    )
    d = _dispatcher({"hooks": {"PreToolUse": [{"matcher": "Bash", "command": f"{sys.executable} {hook}"}]}})
    results = await d.dispatch(HookEvent(name="PreToolUse", context={"tool": "Bash", "args": {}}), matcher="Bash")
    assert len(results) == 1
    assert results[0].block is True
    assert results[0].reason == "nope"


async def test_dispatch_inject_and_rewrite(isolated_home, tmp_path):
    hook = tmp_path / "inject.py"
    hook.write_text(
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'inject': 'extra context', 'rewrite': 'new prompt'}))\n"
    )
    d = _dispatcher({"hooks": {"UserPromptSubmit": [{"command": f"{sys.executable} {hook}"}]}})
    results = await d.dispatch(HookEvent(name="UserPromptSubmit", context={"prompt": "hi"}))
    assert results[0].inject == "extra context"
    assert results[0].rewrite == "new prompt"
    assert results[0].block is False


async def test_dispatch_receives_event_payload(isolated_home, tmp_path):
    # the hook echoes back the tool name it received on stdin
    hook = tmp_path / "echo.py"
    hook.write_text(
        "import sys, json\n"
        "payload = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'inject': payload['tool']}))\n"
    )
    d = _dispatcher({"hooks": {"PreToolUse": [{"command": f"{sys.executable} {hook}"}]}})
    results = await d.dispatch(HookEvent(name="PreToolUse", context={"tool": "write_file", "args": {}}))
    assert results[0].inject == "write_file"


async def test_dispatch_swallows_bad_json(isolated_home, tmp_path):
    hook = tmp_path / "bad.py"
    hook.write_text("print('not json')\n")
    d = _dispatcher({"hooks": {"PreToolUse": [{"command": f"{sys.executable} {hook}"}]}})
    results = await d.dispatch(HookEvent(name="PreToolUse", context={"tool": "Bash", "args": {}}))
    assert results == []


async def test_dispatch_no_hooks_configured(isolated_home):
    d = _dispatcher({})
    results = await d.dispatch(HookEvent(name="PreToolUse", context={"tool": "Bash", "args": {}}))
    assert results == []


def test_matcher_is_case_insensitive(isolated_home):
    """HOOK-01: docs said "Bash" while the tool is `bash`; a copied matcher
    must still fire instead of silently disabling the hook."""
    d = _dispatcher(
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "command": "echo x"},
                    {"matcher": "WRITE_FILE", "command": "echo y"},
                ]
            }
        }
    )
    assert len(d._hooks_for("PreToolUse", "bash")) == 1
    assert len(d._hooks_for("PreToolUse", "write_file")) == 1
    assert len(d._hooks_for("PreToolUse", "read_file")) == 0


async def test_plugin_hooks_can_be_disabled_per_dispatch(isolated_home, tmp_path):
    """定时任务会话（headless shadow session）不加载 Claude Mods 的 plugin
    hooks，但用户 settings.json 里的经典 hooks 必须继续执行（2026-10-28）。"""
    settings_hook = tmp_path / "settings_hook.py"
    settings_hook.write_text(
        "import sys, json\nsys.stdin.read()\nprint(json.dumps({'inject': 'settings'}))\n"
    )
    plugin_hook = tmp_path / "plugin_hook.py"
    plugin_hook.write_text(
        "import sys, json\nsys.stdin.read()\nprint(json.dumps({'inject': 'plugin'}))\n"
    )
    d = _dispatcher({"hooks": {"SessionStart": [{"command": f"{sys.executable} {settings_hook}"}]}})
    assert d.register_plugin(
        "claude-music",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"{sys.executable} {plugin_hook}"}]}
                ]
            }
        },
        str(tmp_path),
    ) == 1
    event = HookEvent(name="SessionStart", context={})
    both = await d.dispatch(event)
    assert {r.inject for r in both} == {"settings", "plugin"}
    only_settings = await d.dispatch(event, plugin_hooks=False)
    assert [r.inject for r in only_settings] == ["settings"]
