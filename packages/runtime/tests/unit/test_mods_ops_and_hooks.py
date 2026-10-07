"""Unit tests for the Python-backed $ ops (mods/ops.py) and the classic-shape
plugin hooks bridging (hooks/dispatcher.py register_plugin + §10 wiring)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ginno_runtime.hooks.dispatcher import HookDispatcher, HookEvent
from ginno_runtime.mods import bridge_utils
from ginno_runtime.mods import ops as mods_ops
from ginno_runtime.mods.ops import OpError

pytestmark = pytest.mark.unit


# ---- ops: op table -------------------------------------------------------------


async def test_op_unknown_ns_and_method_are_named_rejects():
    with pytest.raises(OpError) as e:
        await mods_ops.handle("fs", "read", {}, "s1")
    assert e.value.code == "no-implementation"
    assert "fs.read" in str(e.value)
    with pytest.raises(OpError):
        await mods_ops.handle("session", "launch_missiles", {}, "s1")


async def test_op_requires_session_context():
    with pytest.raises(OpError) as e:
        await mods_ops.handle("session", "id", {}, "")
    assert e.value.code == "not-found"


async def test_session_read_family(isolated_home):
    assert await mods_ops.handle("session", "id", {}, "abc") == "abc"
    assert await mods_ops.handle("session", "cwd", {}, "abc") == ""  # no meta → empty
    assert await mods_ops.handle("session", "root", {}, "abc") == ""
    assert await mods_ops.handle("session", "turns", {}, "abc") == 0  # no checkpoint
    # 未知会话返回零窗口而不是 None:mods 规范里 usage 永远是对象(mod 会解构 .context)
    assert await mods_ops.handle("session", "usage", {}, "abc") == {
        "context": {"tokens": 0, "window": 0},
        "rateLimits": [],
    }
    assert await mods_ops.handle("session", "messages", {}, "abc") == []


def test_to_canonical_shapes():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    assert mods_ops.to_canonical(HumanMessage(content="hi")) == {"role": "user", "text": "hi"}
    ai = AIMessage(
        content="doing",
        tool_calls=[{"name": "bash", "args": {"cmd": "ls"}, "id": "t1"}],
    )
    assert mods_ops.to_canonical(ai) == {
        "role": "assistant",
        "text": "doing",
        "toolUses": [{"tool_use_id": "t1", "tool": "bash", "input": {"cmd": "ls"}}],
    }
    tm = ToolMessage(content="out", tool_call_id="t1")
    assert mods_ops.to_canonical(tm)["role"] == "toolResult"
    assert mods_ops.to_canonical(SystemMessage(content="secret")) is None


# ---- classic plugin hooks: registration ----------------------------------------


def test_register_plugin_parses_classic_shape(tmp_path):
    d = HookDispatcher(settings={})
    root = str(tmp_path / "my-mod")
    n = d.register_plugin(
        "my-mod",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/a.sh"}]}
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/b.sh"}],
                    }
                ],
                "NotAnEvent": [{"hooks": [{"type": "command", "command": "nope"}]}],
            }
        },
        root,
    )
    assert n == 2  # the unknown event name is dropped
    assert d._plugin_hooks["SessionStart"][0]["command"] == f"{root}/hooks/a.sh"
    assert d._hooks_for("PreToolUse", "Bash")[0]["matcher"] == "Bash"
    assert d._hooks_for("PreToolUse", "Write") == []
    assert d._hooks_for("SessionStart", None)[0]["plugin_root"] == root


def test_register_plugin_reregistration_replaces():
    d = HookDispatcher(settings={})
    d.register_plugin(
        "m", {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "old"}]}]}}, "/r"
    )
    d.register_plugin(
        "m", {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "new"}]}]}}, "/r"
    )
    assert d._hooks_for("SessionStart", None) == []
    assert [h["command"] for h in d._hooks_for("SessionEnd", None)] == ["new"]


async def test_dispatch_injects_claude_plugin_root_env(tmp_path):
    hook = tmp_path / "echo_root.py"
    hook.write_text(
        "import sys, json, os\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'inject': os.environ.get('CLAUDE_PLUGIN_ROOT', '')}))\n"
    )
    d = HookDispatcher(settings={})
    d.register_plugin(
        "m",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"{sys.executable} {hook}"}]}
                ]
            }
        },
        str(tmp_path),
    )
    results = await d.dispatch(HookEvent(name="SessionStart", context={"session_id": "s1"}))
    assert results[0].inject == str(tmp_path)


async def test_dispatch_merges_settings_and_plugin_hooks(tmp_path):
    hook = tmp_path / "tag.py"
    hook.write_text(
        "import sys, json, os\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'inject': os.environ.get('GINNO_HOOK_TAG', 'plugin')}))\n"
    )
    settings_hook = tmp_path / "settings_tag.py"
    settings_hook.write_text(
        "import sys, json, os\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'inject': 'settings'}))\n"
    )
    d = HookDispatcher(
        settings={"hooks": {"PreToolUse": [{"command": f"{sys.executable} {settings_hook}"}]}}
    )
    d.register_plugin(
        "m",
        {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": f"{sys.executable} {hook}"}]}
                ]
            }
        },
        str(tmp_path),
    )
    results = await d.dispatch(
        HookEvent(name="PreToolUse", context={"tool": "bash", "args": {}}), matcher="bash"
    )
    assert [r.inject for r in results] == ["settings", "plugin"]  # settings first


# ---- classic plugin hooks: discovery / scan -------------------------------------


def _install_mod(home: Path, name: str, hooks_doc: dict | None) -> Path:
    mod = home / "mods" / name
    (mod / "hooks").mkdir(parents=True, exist_ok=True)
    (mod / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (mod / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0"})
    )
    if hooks_doc is not None:
        (mod / "hooks" / "hooks.json").write_text(json.dumps(hooks_doc))
    return mod


def test_read_classic_hooks_doc_shapes(isolated_home):
    classic = _install_mod(
        isolated_home,
        "classic-mod",
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}},
    )
    assert bridge_utils.read_classic_hooks_doc(classic) is not None
    js_mod = _install_mod(isolated_home, "js-mod", {"modules": [{"path": "./index.js"}]})
    assert bridge_utils.read_classic_hooks_doc(js_mod) is None  # JS shape → broker
    assert bridge_utils.read_classic_hooks_doc(isolated_home / "mods" / "missing") is None


def test_register_classic_plugin_hooks_scans_home(isolated_home):
    _install_mod(
        isolated_home,
        "classic-mod",
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "a"}]}]}},
    )
    _install_mod(
        isolated_home,
        "other-mod",
        {
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "b"}]}]
            }
        },
    )
    _install_mod(isolated_home, "js-mod", {"modules": []})
    d = HookDispatcher(settings={})
    assert bridge_utils.register_classic_plugin_hooks(d) == 2
    assert len(d._hooks_for("SessionStart", None)) == 1
    assert d._hooks_for("PreToolUse", "Write") == []
    # Idempotent: a second registration pass doesn't double the entries.
    assert bridge_utils.register_classic_plugin_hooks(d) == 2
    assert len(d._hooks_for("SessionStart", None)) == 1


def test_register_classic_plugin_hooks_handles_junk(isolated_home):
    mod = isolated_home / "mods" / "broken"
    (mod / "hooks").mkdir(parents=True)
    (mod / "hooks" / "hooks.json").write_text("not json{")
    d = HookDispatcher(settings={})
    assert bridge_utils.register_classic_plugin_hooks(d) == 0


def test_sessionend_is_a_known_hook_event():
    # The bridging wires SessionEnd dispatch; the Literal must carry it.
    from ginno_runtime.hooks.dispatcher import HookEventName

    assert "SessionEnd" in HookEventName.__args__
