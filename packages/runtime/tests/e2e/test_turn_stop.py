"""E2E: user-initiated stop of a running chat turn (WS ``stop`` message).

Covers the stop feature's contract:
* a turn can be stopped mid-tool and mid-stream — the client gets
  ``turn.stopped`` (never an error card), streamed/committed output is kept;
* the persisted state is HEALED: dangling tool_calls gain "(interrupted)"
  ToolMessages so the next turn doesn't 400 at the provider API;
* ``turn_state`` reports not-running afterwards; stop is idempotent/no-op
  when idle; a turn parked at a permission interrupt can also be stopped;
* two tabs stopping at once produce exactly one stop each.

Same infra as test_ws_reconnect_resume.py (ScriptedChatModel + bash sleep
window). NOTE: with turns as background tasks, closing a socket no longer
cancels the turn (production parity) — every test drains to a terminal
event before exiting the client context.
"""

from __future__ import annotations

import json
import time

import pytest
from conftest import events_of, script, script_tool_call

pytestmark = pytest.mark.e2e


@pytest.fixture
def bypass_on(isolated_home):
    """Privileged mode: bash is "ask" in the seeded policy, which would park
    the turn at a permission prompt instead of running the slow tool."""
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(json.dumps(s))


def _drain_stopped(conv):
    """Wait for turn.stopped and assert nothing scary came with it."""
    evs = conv.recv_until("turn.stopped", "error", "message.end")
    assert not events_of(evs, "error"), evs
    assert not events_of(evs, "message.end"), evs
    assert evs[-1]["event"] == "turn.stopped"
    return evs


def _wait_idle(conv):
    """Probe until the server reports no running turn (mirrors what the
    frontend effectively does before re-enabling send)."""
    for _ in range(50):
        conv.send({"type": "turn_state"})
        state = conv.recv_until("turn.state")[-1]
        if not state["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("turn still running after stop")


# --------------------------------------------------------------------------- #
# (a) stop mid-tool-execution: heal + a clean next turn
# --------------------------------------------------------------------------- #
def test_stop_mid_tool_heals_state_and_next_turn_works(
    client, create_session, ws_conv, bypass_on
):
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "sleep 1.5; echo MARK"})]),
        script(text="all done"),
    ]
    sid = create_session(model, agent_id="dev")

    with ws_conv(sid) as conv:
        conv.invoke("run the slow tool")
        first = conv.recv_until("tool.start", "error")
        assert [e["event"] for e in first][-1] == "tool.start"
        # Let the agent superstep commit (chunks are instant; the commit is
        # microseconds later) — stop then lands inside the bash sleep window.
        time.sleep(0.5)
        conv.send({"type": "stop"})
        _drain_stopped(conv)
        _wait_idle(conv)

        # The dangling tool call was healed — history shows "(interrupted)".
        msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
        assert any("(interrupted)" in str(m.get("blocks")) for m in msgs), msgs

        # The next turn starts clean (would 400 at a real provider without
        # the heal) and consumes the second script entry.
        conv.invoke("carry on")
        rest = conv.recv_until("message.end", "error")
        assert not events_of(rest, "error")
        assert rest[-1]["event"] == "message.end"

    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert any("all done" in str(m.get("blocks")) for m in msgs)


# --------------------------------------------------------------------------- #
# (c) stop while idle is a silent no-op
# --------------------------------------------------------------------------- #
def test_stop_when_idle_is_noop(create_session, ws_conv):
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")
        conv.send({"type": "stop"})
        conv.send({"type": "ping"})
        evs = conv.recv_until("pong", "error")
        assert not events_of(evs, "error")
        assert not events_of(evs, "turn.stopped")
        assert evs[-1]["event"] == "pong"
        # …and the next turn is not killed by a stale stop signal
        conv.invoke("hello again")
        rest = conv.recv_until("message.end", "error")
        assert not events_of(rest, "error")
        assert rest[-1]["event"] == "message.end"


# --------------------------------------------------------------------------- #
# (d) stop a turn parked at a permission interrupt
# --------------------------------------------------------------------------- #
def test_stop_at_permission_interrupt(client, create_session, ws_conv):
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "x"})]),
        script(text="after stop"),
    ]
    sid = create_session(model, agent_id="dev")  # conftest client: bypass OFF

    with ws_conv(sid) as conv:
        conv.invoke("write a file")
        p = conv.recv_until("permission.request", "error")
        assert events_of(p, "permission.request")
        conv.send({"type": "stop"})
        _drain_stopped(conv)

        # Healed: the tool block renders "(interrupted)" in history.
        msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
        assert any("(interrupted)" in str(m.get("blocks")) for m in msgs), msgs

        # A stale late permission_response must be ignored (socket healthy).
        conv.respond_permission("allow")
        conv.send({"type": "ping"})
        evs = conv.recv_until("pong", "error")
        assert not events_of(evs, "error")
        assert evs[-1]["event"] == "pong"

        # Next turn works (the interrupt was cleared by the heal).
        conv.invoke("carry on")
        rest = conv.recv_until("message.end", "error")
        assert not events_of(rest, "error")
        assert rest[-1]["event"] == "message.end"


# --------------------------------------------------------------------------- #
# (e) double stop from two tabs: exactly one turn.stopped per socket
# --------------------------------------------------------------------------- #
def test_double_stop_from_two_sockets(client, create_session, ws_conv, bypass_on):
    model = [
        script(tool_calls=[script_tool_call("bash", {"command": "sleep 1.5"})]),
        script(text="done"),
    ]
    sid = create_session(model, agent_id="dev")

    with ws_conv(sid) as conv1, ws_conv(sid) as conv2:
        conv1.invoke("run the slow tool")
        conv1.recv_until("tool.start", "error")
        time.sleep(0.5)
        conv1.send({"type": "stop"})
        conv2.send({"type": "stop"})
        e1 = _drain_stopped(conv1)
        e2 = conv2.recv_until("turn.stopped", "error", "message.end")
        assert len(events_of(e1, "turn.stopped")) == 1
        assert len(events_of(e2, "turn.stopped")) == 1
        assert not events_of(e2, "error")
        _wait_idle(conv1)
