"""E2E: a mid-turn socket drop must not lose the rest of the turn.

Regression suite for the 2026-08-05 incident (turn c1649937): the user asked
to install skills; the turn ran to completion server-side, but the client's
socket died mid-turn and everything after the drop was invisible — turn
events were tied to the ONE socket that sent the invoke, and the frontend
wiped its in-flight stream state on every reconnect ("output stops halfway").

Now:
* turn events broadcast to EVERY live socket of the session, so a reconnect
  resumes the running stream;
* a ``turn_state`` query lets the reconnecting client distinguish "stream
  resumes" from "turn gone — reconcile from history";
* a duplicated permission_response (two tabs saw the broadcast prompt) is
  ignored instead of double-resuming the graph.
"""

from __future__ import annotations

import pytest
from conftest import events_of, script, script_tool_call

from ginno_runtime import paths, server, server_shared

pytestmark = pytest.mark.e2e


@pytest.fixture
def fast_prune(monkeypatch):
    """Shrink the stuck-socket send timeout so an unread socket is pruned
    quickly (production default is 5s; see server_shared._WS_SEND_TIMEOUT_S)."""
    monkeypatch.setattr(server_shared, "_WS_SEND_TIMEOUT_S", 0.5)


# --------------------------------------------------------------------------- #
# broadcast: the turn keeps streaming on a socket opened after the drop
# --------------------------------------------------------------------------- #
def test_midturn_reconnect_keeps_receiving_stream(
    client, create_session, ws_conv, fast_prune, isolated_home
):
    # privileged mode: bash is "ask" in the seeded policy, which would park the
    # turn at a permission prompt instead of running the slow tool
    import json as _json

    sp = isolated_home / "settings.json"
    s = _json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(_json.dumps(s))

    model = [
        # a slow tool holds the turn open long enough to abandon + reconnect
        script(tool_calls=[script_tool_call("bash", {"command": "sleep 1.5; echo MARK"})]),
        script(text="all done"),
    ]
    sid = create_session(model, agent_id="dev")

    with ws_conv(sid) as conv1:
        conv1.invoke("run the slow tool")
        first = conv1.recv_until("tool.start", "error")
        assert [e["event"] for e in first][-1] == "tool.start"

        # The client abandons this socket mid-turn and reconnects. NOTE: the
        # abandoned socket stays OPEN here — closing the TestClient context
        # tears down the connection's portal, which cancels the turn running
        # inside that handler (a test-infra artifact; under uvicorn a client
        # disconnect never cancels a running turn — incident c1649937 proved
        # the turn completes server-side). The abandoned socket stops reading,
        # so the next broadcast prunes it after the (shrunk) send timeout.
        with ws_conv(sid) as conv2:
            # the server answers the post-reconnect probe: turn still running
            conv2.send({"type": "turn_state"})
            # the rest of the turn arrives on the NEW socket (turn.state,
            # tool.end and message.end may interleave with keepalives)
            rest = conv2.recv_until("message.end", "error")
            assert not events_of(rest, "error")
            states = events_of(rest, "turn.state")
            assert states and states[0]["running"] is True
            tool_ends = events_of(rest, "tool.end")
            assert any("MARK" in e["content"] for e in tool_ends)
            assert rest[-1]["event"] == "message.end"

    # turn fully completed server-side (history intact despite the drop)
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert any("all done" in str(m.get("blocks")) for m in msgs)


def test_turn_state_false_when_idle(create_session, ws_conv):
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")
        # turn over → probe reports not running (client would reconcile
        # against /history instead of waiting for a dead stream)
        conv.send({"type": "turn_state"})
        state = conv.recv_until("turn.state")[-1]
        assert state["running"] is False


def test_ping_after_turn_still_answered(create_session, ws_conv):
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")
        conv.send({"type": "ping"})
        assert conv.recv_until("pong")[-1]["event"] == "pong"


# --------------------------------------------------------------------------- #
# duplicated permission responses (broadcast prompt reaches every tab)
# --------------------------------------------------------------------------- #
def test_second_permission_response_is_ignored(client, create_session, ws_conv, fast_prune):
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "x"})]),
        script(text="wrote it"),
    ]
    sid = create_session(model, agent_id="dev")  # conftest client: bypass OFF

    with ws_conv(sid) as conv1, ws_conv(sid) as conv2:
        conv1.invoke("write a file")
        # BOTH sockets see the prompt (broadcast to every connected socket)
        p1 = conv1.recv_until("permission.request", "error")
        assert events_of(p1, "permission.request")
        p2 = conv2.recv_until("permission.request", "error")
        assert events_of(p2, "permission.request")

        # first response resumes the turn (conv1 reads it; conv2 is not
        # reading, so it is pruned from the broadcast set — expected)
        conv1.respond_permission("allow")
        end1 = conv1.recv_until("message.end", "error")
        assert not events_of(end1, "error")
        assert end1[-1]["event"] == "message.end"

        # a stale second response (the other tab reacts late) must be
        # ignored — no crash, no error event, socket stays healthy (pong)
        conv2.respond_permission("allow")
        conv2.send({"type": "ping"})
        evs = conv2.recv_until("pong", "error")
        assert not events_of(evs, "error")
        assert evs[-1]["event"] == "pong"

    assert (paths.session_files_dir("default", sid) / "out.txt").read_text() == "x"
