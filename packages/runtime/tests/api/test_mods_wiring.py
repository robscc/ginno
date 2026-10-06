"""API-level wiring tests for the mods event taps (claude-code-mods-design.md
§5.3): the five P0 mount sites observed through the real FastAPI stack with a
stub ModChannel — no broker, no node, no network."""

from __future__ import annotations

import pytest

from ginno_runtime.mods import channel as mods_channel
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.api


class StubChannel:
    """Records every event the taps dispatch; optionally denies a tool."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []
        self.deny_tool: str | None = None

    def ensure_started(self):
        pass

    def active_for(self, event):
        return True

    async def dispatch_event(self, session_id, event, payload, deadline_ms=10_000):
        self.events.append((session_id, event, payload))
        if event == "tool.call" and payload.get("tool") == self.deny_tool:
            return {**payload, "deny": "mods say no"}
        return payload


@pytest.fixture
def stub_channel(monkeypatch):
    stub = StubChannel()
    monkeypatch.setattr(mods_channel, "get_channel", lambda: stub)
    return stub


def test_session_start_and_end_dispatched(client, create_session, stub_channel):
    sid = create_session(script(text="hi"))
    start_payloads = [p for s, ev, p in stub_channel.events if ev == "session.start" and s == sid]
    assert start_payloads
    r = client.delete(f"/api/sessions/{sid}")
    assert r.json().get("ok") is True
    end_events = [(s, p) for s, ev, p in stub_channel.events if ev == "session.end"]
    assert end_events and end_events[0][0] == sid


def test_prompt_submit_and_turn_events(client, create_session, ws_conv, stub_channel):
    sid = create_session([script(text="done")])
    stub_channel.events.clear()  # drop the create-time session.start
    with ws_conv(sid) as conv:
        conv.invoke("hello mods")
        conv.recv_until("message.end")
    names = [ev for _, ev, _ in stub_channel.events]
    # prompt.submit before the turn; turn.start / turn.complete ride the turn.
    assert names == ["prompt.submit", "turn.start", "turn.complete"]
    _, _, ps = stub_channel.events[0]
    assert ps["text"] == "hello mods"
    _, _, tc = stub_channel.events[2]
    assert tc["reason"] == "answer"
    assert tc["answer"] == "done"
    assert tc["isAborted"] is False


def test_tool_call_deny_blocks_execution(client, create_session, ws_conv, stub_channel):
    stub_channel.deny_tool = "bash"
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "echo pwned"})]),
        script(text="ok"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("run it")
        conv.recv_until("message.end")
    tool_events = [p for _, ev, p in stub_channel.events if ev == "tool.call"]
    assert tool_events and tool_events[0]["tool"] == "bash"
    # The blocked bubble (same shape as the hooks block) became the answer —
    # the tool never ran (its output never appears).
    complete = [p for _, ev, p in stub_channel.events if ev == "turn.complete"][0]
    assert "mod denied" in complete["answer"]
