"""P1 mods contracts (claude-code-mods-design.md §5.3/§15.5/§15.6): the
prompt.submit / tool.call / tool.check / command.run consumption shapes plus
the mod slash-command registry — all against a stub ModChannel, no broker."""

from __future__ import annotations

import pytest

from ginno_runtime.commands import mod_commands, resolver
from ginno_runtime.mods import channel as mods_channel
from ginno_runtime.mods import events as mods_events
from ginno_runtime.mods import ops


class SettleChannel:
    """Replies to every dispatch with a scripted settle value."""

    def __init__(self, settle=None):
        self.settle = settle if settle is not None else {}
        self.events: list[tuple[str, dict]] = []

    def ensure_started(self):
        pass

    def active_for(self, event):
        return True

    async def dispatch_event(self, session_id, event, payload, deadline_ms=10_000):
        self.events.append((event, payload))
        if isinstance(self.settle, dict) and event in self.settle:
            return {"sessionId": session_id, **self.settle[event]}
        return payload


@pytest.fixture
def settle_channel(monkeypatch):
    def _make(settle=None):
        ch = SettleChannel(settle)
        monkeypatch.setattr(mods_channel, "get_channel", lambda: ch)
        return ch

    return _make


# ---- prompt.submit (§15.6) -----------------------------------------------------


async def test_prompt_submit_text_rewrite(settle_channel):
    settle_channel({"prompt.submit": {"text": "rewritten"}})
    r = await mods_events.dispatch_prompt_submit("s1", "original")
    assert r.text == "rewritten" and not r.blocked and r.context is None


async def test_prompt_submit_drop_blocks(settle_channel):
    settle_channel({"prompt.submit": {"drop": "not allowed"}})
    r = await mods_events.dispatch_prompt_submit("s1", "hi")
    assert r.blocked and r.source == "mod"


async def test_prompt_submit_context_keeps_string_lines_only(settle_channel):
    settle_channel(
        {"prompt.submit": {"context": ["line one", 42, "line two"], "mod": "blast-radius"}}
    )
    r = await mods_events.dispatch_prompt_submit("s1", "hi")
    assert r.context == "line one\nline two"
    assert r.mod == "blast-radius"
    assert not r.blocked


async def test_prompt_submit_non_string_text_kept(settle_channel):
    settle_channel({"prompt.submit": {"text": 123}})
    r = await mods_events.dispatch_prompt_submit("s1", "original")
    assert r.text == "original"


async def test_prompt_submit_nested_answer_deny(settle_channel):
    # Both deny spellings appear in the wild (§6.1 settle convention).
    settle_channel({"prompt.submit": {"answer": {"drop": True}}})
    r = await mods_events.dispatch_prompt_submit("s1", "hi")
    assert r.blocked


async def test_prompt_submit_classic_block_source(settle_channel, monkeypatch):
    class R:
        block = True
        rewrite = ""

    import ginno_runtime.mods.events as ev

    async def fake_classic(name, context, matcher=None):
        return [R()]

    monkeypatch.setattr(ev, "_classic_dispatch", fake_classic)
    r = await ev.dispatch_prompt_submit("s1", "hi")
    assert r.blocked and r.source == "classic"


# ---- tool.call (§15.5 contract) -----------------------------------------------


async def test_tool_call_deny_reason(settle_channel):
    settle_channel({"tool.call": {"deny": "nope"}})
    action = await mods_events.dispatch_tool_call("s1", "bash", {"command": "ls"})
    assert action == {"deny": "nope"}


async def test_tool_call_deny_bool_normalized(settle_channel):
    settle_channel({"tool.call": {"deny": True}})
    action = await mods_events.dispatch_tool_call("s1", "bash", {})
    assert action == {"deny": "denied by mod"}


async def test_tool_call_args_rewrite(settle_channel):
    settle_channel({"tool.call": {"args": {"command": "echo safe"}}})
    action = await mods_events.dispatch_tool_call("s1", "bash", {"command": "rm -rf /"})
    assert action == {"args": {"command": "echo safe"}}


async def test_tool_call_result_takeover(settle_channel):
    settle_channel({"tool.call": {"result": {"value": {"ok": True}, "isError": False}}})
    action = await mods_events.dispatch_tool_call("s1", "bash", {})
    assert action["result"]["value"] == {"ok": True}


async def test_tool_call_inject_coexists_with_args(settle_channel):
    settle_channel({"tool.call": {"args": {"a": 1}, "inject": "ctx"}})
    action = await mods_events.dispatch_tool_call("s1", "bash", {"b": 2})
    assert action["args"] == {"a": 1} and action["inject"] == "ctx"


async def test_tool_call_no_answer_returns_none(settle_channel):
    settle_channel({"tool.call": {}})
    assert await mods_events.dispatch_tool_call("s1", "bash", {}) is None


# ---- tool.check (§5.3 P1) ------------------------------------------------------


async def test_tool_check_decision_passes_through(settle_channel):
    settle_channel({"tool.check": {"decision": "deny", "reason": "too risky"}})
    out = await mods_events.dispatch_tool_check("s1", "bash", {})
    assert out == {"decision": "deny", "reason": "too risky"}


async def test_tool_check_bad_decision_is_none(settle_channel):
    settle_channel({"tool.check": {"decision": "maybe"}})
    assert await mods_events.dispatch_tool_check("s1", "bash", {}) is None


async def test_tool_check_no_registration_is_none(settle_channel):
    settle_channel({})
    assert await mods_events.dispatch_tool_check("s1", "bash", {}) is None


# ---- command.run ----------------------------------------------------------------


async def test_command_run_reply(settle_channel):
    ch = settle_channel({"command.run": {"reply": "42 tickets open"}})
    reply = await mods_events.dispatch_command_run("s1", "tickets", "")
    assert reply == "42 tickets open"
    assert ch.events[-1][1]["command"] == "tickets"


async def test_command_run_no_reply_is_none(settle_channel):
    settle_channel({"command.run": {}})
    assert await mods_events.dispatch_command_run("s1", "tickets", "") is None


# ---- command registry + ops -----------------------------------------------------


def test_command_register_via_op():
    import asyncio

    entry = asyncio.run(
        ops.handle("command", "register", {"name": "tickets", "description": "list"}, "s1", mod="helpdesk")
    )
    assert entry["ok"] is True and entry["mod"] == "helpdesk"
    assert mod_commands.lookup("tickets")["mod"] == "helpdesk"
    assert [c["name"] for c in mod_commands.list_commands()] == ["tickets"]
    mod_commands.clear()


def test_command_register_rejects_bad_name():
    import asyncio

    with pytest.raises(ops.OpError):
        asyncio.run(ops.handle("command", "register", {"name": "bad name!"}, "s1", mod="m"))


def test_command_register_requires_mod():
    import asyncio

    with pytest.raises(ops.OpError):
        asyncio.run(ops.handle("command", "register", {"name": "x"}, "s1", mod=""))


def test_unregister_mod_drops_its_commands():
    mod_commands.register("a1", "mod-a")
    mod_commands.register("b1", "mod-b")
    mod_commands.unregister_mod("mod-a")
    assert mod_commands.lookup("a1") is None
    assert mod_commands.lookup("b1") is not None
    mod_commands.clear()


# ---- channel notify routing: mod.ask is session-scoped --------------------------


def test_mod_ask_notify_routed_to_its_session():
    import asyncio

    from ginno_runtime.mods.channel import ModChannel

    ch = ModChannel(sock_path="/nonexistent")
    seen: list[tuple[str | None, str, dict]] = []

    async def fake_broadcast(session_id, event, data):
        seen.append((session_id, event, data))

    ch._broadcast = fake_broadcast  # type: ignore[method-assign]
    asyncio.run(
        ch._handle_notify(
            {
                "kind": "notify",
                "method": "mod.ask",
                "session": "s9",
                "args": {"id": "a1", "mod": "m", "message": "go?", "choices": ["y", "n"]},
            }
        )
    )
    assert seen == [
        ("s9", "mod.ask", {"id": "a1", "mod": "m", "message": "go?", "choices": ["y", "n"]})
    ]


def test_mod_ask_notify_session_may_ride_args():
    import asyncio

    from ginno_runtime.mods.channel import ModChannel

    ch = ModChannel(sock_path="/nonexistent")
    seen: list[tuple[str | None, str]] = []

    async def fake_broadcast(session_id, event, data):
        seen.append((session_id, event))

    ch._broadcast = fake_broadcast  # type: ignore[method-assign]
    asyncio.run(
        ch._handle_notify(
            {
                "kind": "notify",
                "method": "mod.ask",
                "args": {"id": "a1", "session": "s7", "message": "go?", "choices": []},
            }
        )
    )
    assert seen == [("s7", "mod.ask")]


def test_mod_ask_notify_mod_name_field_folded_into_args():
    """The Notify frame spells the owning mod "mod_name" (Call frames "mod",
    broker seam 2026-10-08) — the WS event must carry it as "mod"."""
    import asyncio

    from ginno_runtime.mods.channel import ModChannel

    ch = ModChannel(sock_path="/nonexistent")
    seen: list[tuple[str | None, str, dict]] = []

    async def fake_broadcast(session_id, event, data):
        seen.append((session_id, event, data))

    ch._broadcast = fake_broadcast  # type: ignore[method-assign]
    asyncio.run(
        ch._handle_notify(
            {
                "kind": "notify",
                "method": "mod.ask",
                "session": "s9",
                "mod_name": "helpdesk",
                "args": {"id": "a1", "message": "go?", "choices": ["y"]},
            }
        )
    )
    assert seen == [
        ("s9", "mod.ask", {"id": "a1", "message": "go?", "choices": ["y"], "mod": "helpdesk"})
    ]


# ---- resolver: /name falls through to mod commands ------------------------------


def test_resolver_mod_command_after_builtins_and_skills(monkeypatch, tmp_path):
    mod_commands.register("deploy", "shipper")
    try:
        session = {"project_slug": "default", "workspace": str(tmp_path)}
        plan = resolver.resolve_turn({"message": "/deploy prod"}, session)
        assert plan.mod_command == ("deploy", "prod")
        # Unknown /name still passes through as a plain message.
        plan2 = resolver.resolve_turn({"message": "/not-a-command"}, session)
        assert plan2.mod_command is None
    finally:
        mod_commands.clear()


def test_resolver_builtin_shadows_mod_command(monkeypatch, tmp_path):
    mod_commands.register("help", "greedy")
    try:
        session = {"project_slug": "default", "workspace": str(tmp_path)}
        plan = resolver.resolve_turn({"message": "/help"}, session)
        assert plan.mod_command is None  # builtin kept priority (P1 不拦截)
    finally:
        mod_commands.clear()
