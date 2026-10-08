"""API-level wiring tests for the P1 mods contracts (claude-code-mods-design.md
§5.3): prompt.submit drop/context, tool.call result-takeover + args rewrite,
tool.check deny and the mod slash-command round trip — through the real WS
stack with a stub ModChannel (no broker, no node)."""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime.commands import mod_commands
from ginno_runtime.mods import channel as mods_channel
from ginno_runtime.mods import ops
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.api


class StubChannel:
    """Scripted per-event settle values + event recording, like the P0 one."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []
        self.settle: dict[str, dict] = {}

    def ensure_started(self):
        pass

    def active_for(self, event):
        return True

    async def dispatch_event(self, session_id, event, payload, deadline_ms=10_000):
        self.events.append((session_id, event, payload))
        extra = self.settle.get(event)
        if isinstance(extra, dict) and extra.get("_tool") == payload.get("tool"):
            return {**payload, **{k: v for k, v in extra.items() if not k.startswith("_")}}
        return payload


@pytest.fixture
def stub_channel(monkeypatch):
    stub = StubChannel()
    monkeypatch.setattr(mods_channel, "get_channel", lambda: stub)
    return stub


def _run_until_end(conv, on_permission="allow") -> list[dict]:
    """Drive one invoke to message.end, answering policy interrupts (the
    rewritten call still goes through the permission policy — §5.3)."""
    events: list[dict] = []
    while True:
        ev = conv.recv()
        events.append(ev)
        if ev.get("event") == "permission.request":
            conv.respond_permission(on_permission)
        elif ev.get("event") == "message.end":
            return events


def test_prompt_submit_drop_ends_turn_with_notice(client, create_session, ws_conv, stub_channel):
    sid = create_session(script(text="never runs"))
    stub_channel.settle["prompt.submit"] = {"drop": "no prompts today"}
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        events = conv.recv_until("message.end")
    names = [e.get("event") for e in events]
    assert "notice" in names and "message.end" in names
    # The turn never started: no model answer, no turn.complete.
    assert "turn.start" not in names and "message.delta" not in names
    notice = next(e for e in events if e.get("event") == "notice")
    assert notice["i18n_key"] == "chat.mods.promptBlocked"


def test_prompt_submit_context_injected_as_steer_companion(
    client, create_session, ws_conv, stub_channel
):
    sid = create_session(script(text="got it"))
    stub_channel.settle["prompt.submit"] = {"context": ["sensitive note"], "mod": "memo"}
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end")
    # The turn ran (rewrite/context never blocks) and the companion message —
    # origin-labeled, steer-marked — is persisted right after the user turn.
    r = client.get(f"/api/sessions/{sid}/history").json()
    blob = str(r)
    assert 'Message from the "memo" mod:' in blob
    assert "sensitive note" in blob
    # …and the prompt itself was delivered unchanged.
    assert "hello" in blob


def test_tool_call_result_takeover(client, create_session, ws_conv, stub_channel):
    stub_channel.settle["tool.call"] = {
        "_tool": "bash",
        "result": {"value": "mod was here"},
    }
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "echo pwned"})]),
        script(text="done"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("run it")
        events = conv.recv_until("message.end")
    ends = [e for e in events if e.get("event") == "tool.end" and e.get("id")]
    assert ends and ends[0]["content"] == "mod was here"
    # The tool never executed — the takeover value is the ONLY tool.end with
    # an id (the hooks/policy deny bubbles carry name, not id).
    assert len(ends) == 1
    complete = [p for _, ev, p in stub_channel.events if ev == "turn.complete"][0]
    assert complete["reason"] == "answer"


def test_tool_call_args_rewrite_reruns(client, create_session, ws_conv, stub_channel):
    stub_channel.settle["tool.call"] = {"_tool": "bash", "args": {"command": "echo rewritten"}}
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "echo pwned"})]),
        script(text="done"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("run it")
        events = _run_until_end(conv)
    # The tool executed with the REWRITTEN args — its output says so.
    ends = [e for e in events if e.get("event") == "tool.end" and e.get("id")]
    assert ends and "rewritten" in ends[0]["content"]
    assert "pwned" not in ends[0]["content"]
    # …and the policy saw the rewritten args (its ask carried them).
    asks = [e for e in events if e.get("event") == "permission.request"]
    assert asks and asks[0]["args"] == {"command": "echo rewritten"}


def test_tool_check_deny_blocks_execution(client, create_session, ws_conv, stub_channel):
    stub_channel.settle["tool.check"] = {"_tool": "bash", "decision": "deny", "reason": "too risky"}
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "echo pwned"})]),
        script(text="ok"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("run it")
        events = conv.recv_until("message.end")
    ends = [e for e in events if e.get("event") == "tool.end"]
    assert ends and "mod denied" in ends[0]["content"] and "too risky" in ends[0]["content"]
    # The tool.call chain still ran (check comes after it), the tool did not.
    assert any(ev == "tool.call" for _, ev, _ in stub_channel.events)


def test_tool_check_allow_falls_through_to_policy(client, create_session, ws_conv, stub_channel):
    stub_channel.settle["tool.check"] = {"_tool": "bash", "decision": "allow"}
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "echo fine"})]),
        script(text="done"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("run it")
        events = _run_until_end(conv)
    ends = [e for e in events if e.get("event") == "tool.end" and e.get("id")]
    assert ends and "fine" in ends[0]["content"]


def test_mod_slash_command_round_trip(client, create_session, ws_conv, stub_channel):
    asyncio.run(
        ops.handle("command", "register", {"name": "tickets", "description": "d"}, "s1", mod="helpdesk")
    )
    stub_channel.settle["command.run"] = {"reply": "42 open tickets"}
    sid = create_session(script(text="never runs"))
    with ws_conv(sid) as conv:
        conv.invoke("/tickets all")
        events = conv.recv_until("message.end")
    names = [e.get("event") for e in events]
    assert "notice" in names and "message.end" in names
    assert "turn.start" not in names  # short-circuit: no graph turn
    notice = next(e for e in events if e.get("event") == "notice")
    assert notice["message"] == "42 open tickets"
    run = [p for _, ev, p in stub_channel.events if ev == "command.run"]
    assert run and run[0]["command"] == "tickets" and run[0]["args"] == "all"
    mod_commands.clear()
