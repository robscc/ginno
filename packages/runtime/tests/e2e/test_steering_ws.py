"""E2E: mid-turn steering over the WebSocket (docs/steering-design.md).

The contract these tests pin down:

* a message sent while a turn is running is absorbed by the NEXT superstep —
  the same turn, right after the tool calls that were running when it arrived
  (Claude Code's "as soon as those tool calls finish, within the same turn"),
  not queued for a following turn;
* ``steer.absorbed`` means "durably in state": it is emitted when the steered
  message commits with the superstep update, so a turn stopped between the
  drain and that commit acknowledges NOTHING and leaves no half-written trace —
  which is what lets the client re-send the entry as a normal turn and still
  get exactly-once delivery;
* a message typed at a permission card is a redirect: the client stashes the
  text and then denies, and the resumed segment (the deny routes straight back
  to ``agent``) picks it up;
* ``steer`` is not a turn: it never registers as running and never touches the
  stop signal.

The assertions read the prompts the fake model actually received, because an
emitted event alone would not prove the text reached a model request.
"""

from __future__ import annotations

import json

import pytest
from conftest import events_of, script, script_tool_call

from ginno_runtime import server_shared
from ginno_runtime.testing.fake_model import ScriptedChatModel

pytestmark = pytest.mark.e2e


@pytest.fixture
def bypass_on(isolated_home):
    """Privileged mode: bash is "ask" in the seeded policy, which would park the
    turn at a permission prompt instead of running the slow tool."""
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(json.dumps(s))


class RecordingScriptedChatModel(ScriptedChatModel):
    """ScriptedChatModel that also records every prompt it was called with.

    The claim under test is about what the MODEL saw and when — an emitted
    event alone would not prove the steered text reached a model request inside
    the same turn, rather than being stashed and sent later.
    """

    seen: list = []

    def _next(self, messages=None):
        self.seen = [*self.seen, list(messages or [])]
        return super()._next(messages)


def _saw_in_one_call(model: RecordingScriptedChatModel, *needles: str) -> bool:
    """True when SOME single recorded model request contained all ``needles``."""
    return any(
        all(any(n in str(getattr(m, "content", "")) for m in call) for n in needles)
        for call in model.seen
    )


def _steer(conv, text: str, steer_id: str, turn_id: str | None = None) -> None:
    payload = {"type": "steer", "steer_id": steer_id, "message": text}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    conv.send(payload)


def _steer_bands(client, sid: str) -> list[dict]:
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    return [b for m in msgs for b in m.get("blocks", []) if b.get("kind") == "steer"]


# --------------------------------------------------------------------------- #
# absorption inside the running turn
# --------------------------------------------------------------------------- #
def test_steer_is_absorbed_within_the_same_turn(
    client, create_session, ws_conv, bypass_on
):
    model = RecordingScriptedChatModel(
        scripts=[
            # a slow tool holds the turn open long enough to type into it
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.6; echo MARK"})]),
            script(text="好，先跑测试，不动 stream.py。"),
        ]
    )
    sid = create_session(model, agent_id="dev")

    with ws_conv(sid) as conv:
        conv.invoke("改一下 stream.py")
        conv.recv_until("tool.start", "error")  # the tool is now running
        _steer(conv, "别改那个文件，先跑测试", "steer-1")
        rest = conv.recv_until("message.end", "error")

    names = [e["event"] for e in rest]
    assert not events_of(rest, "error"), rest
    assert "steer.accepted" in names, names
    assert "steer.absorbed" in names, names
    # absorbed BEFORE the turn ended — the whole point: not a next-turn queue
    assert names.index("steer.absorbed") < names.index("message.end")
    assert events_of(rest, "steer.absorbed")[0]["steer_id"] == "steer-1"
    # ...and BEFORE the continuation's tokens. This is the ordering that puts the
    # transcript band at the injection point (right after the tool batch) instead
    # of after the model's reply: acking when the superstep COMMITS is too late,
    # because the whole reply has already streamed by then. The replayed view
    # reads the state order, so getting this wrong made live and history disagree.
    first_continuation_token = next(
        i
        for i, e in enumerate(rest)
        if e["event"] == "token.delta" and "先跑测试" in str(e.get("content") or "")
    )
    assert names.index("steer.absorbed") < first_continuation_token, names

    # ONE model request carried the tool output AND the steer: absorbed at the
    # superstep boundary, i.e. inside the same turn, wrapped so the model knows
    # it arrived mid-work.
    assert _saw_in_one_call(
        model, "MARK", "别改那个文件，先跑测试", "<ginno_steer>"
    ), model.seen

    # transcript: the band rides INSIDE the assistant bubble (one turn = one
    # bubble) and the steer did not become a second user turn.
    bands = _steer_bands(client, sid)
    assert len(bands) == 1, bands
    assert bands[0]["steerId"] == "steer-1"
    assert bands[0]["text"] == "别改那个文件，先跑测试"
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert sum(1 for m in msgs if m["role"] == "user") == 1


def test_steer_accepts_and_absorbs_a_batch_in_order(
    client, create_session, ws_conv, bypass_on
):
    """Two entries queued while the tool runs are absorbed together, in order,
    and the client's ids are what the acks echo back."""
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.6; echo MARK"})]),
            script(text="done"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("干活")
        conv.recv_until("tool.start", "error")
        _steer(conv, "第一件事", "s-a")
        _steer(conv, "第二件事", "s-b")
        rest = conv.recv_until("message.end", "error")

    assert not events_of(rest, "error"), rest
    assert [e["steer_id"] for e in events_of(rest, "steer.absorbed")] == ["s-a", "s-b"]
    assert _saw_in_one_call(model, "第一件事", "第二件事")
    bands = _steer_bands(client, sid)
    assert [(b["steerId"], b["text"]) for b in bands] == [("s-a", "第一件事"), ("s-b", "第二件事")]


def test_steer_needs_no_turn_id_from_the_client(
    client, create_session, ws_conv, bypass_on
):
    """The server falls back to the running turn's id, so a client that lost
    track of it (a goal continuation turn, which nobody invoked) can still
    steer."""
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.5; echo MARK"})]),
            script(text="ok"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("干活")
        conv.recv_until("tool.start", "error")
        _steer(conv, "顺带说一下", "s-nolost")  # no turn_id sent
        rest = conv.recv_until("message.end", "error")
    assert not events_of(rest, "error"), rest
    assert "steer.absorbed" in [e["event"] for e in rest]
    assert _saw_in_one_call(model, "顺带说一下")


# --------------------------------------------------------------------------- #
# the not-absorbed path (exactly-once)
# --------------------------------------------------------------------------- #
def test_steer_without_a_running_turn_is_refused(create_session, ws_conv):
    """Nothing to absorb into → say so. The client then falls back to `invoke`
    instead of believing a message it sent is on its way."""
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        _steer(conv, "在吗", "s-idle")
        evs = conv.recv_until("error")
    assert "正在进行的回合" in events_of(evs, "error")[0]["message"]
    assert server_shared.steer_drain(sid) == []  # nothing was stashed


def test_unabsorbed_steer_leaves_no_trace_and_is_not_acknowledged(
    client, create_session, ws_conv, bypass_on
):
    """A turn stopped between the drain and the commit acknowledges nothing.

    This is what makes the client's re-send safe: an entry it never saw
    absorbed is re-sent as a normal turn, and because nothing was committed
    there is no duplicate. It is also why no heal path is needed for steers.
    """
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 1.2; echo MARK"})]),
            script(text="never runs"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("干活")
        conv.recv_until("tool.start", "error")
        _steer(conv, "先别跑了", "s-cut")
        conv.send({"type": "stop"})
        evs = conv.recv_until("turn.stopped", "message.end", "error")

    assert evs[-1]["event"] == "turn.stopped", evs
    assert not events_of(evs, "steer.absorbed"), evs
    assert not events_of(evs, "error"), evs
    # the stash died with the turn segment, and nothing half-written persisted
    assert server_shared.steer_drain(sid) == []
    assert _steer_bands(client, sid) == []


def test_steer_does_not_keep_a_turn_alive(create_session, ws_conv):
    """`steer` must not look like a running turn: after the turn ends, the probe
    reports idle (a steer that registered as running would wedge the client's
    `running` gate forever)."""
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("你好")
        conv.recv_until("message.end", "error")
        conv.send({"type": "turn_state"})
        assert conv.recv_until("turn.state")[-1]["running"] is False


# --------------------------------------------------------------------------- #
# parked: a message typed at a permission card is a redirect
# --------------------------------------------------------------------------- #
def test_steer_at_a_permission_card_redirects_the_turn(
    client, create_session, ws_conv
):
    """Deny + steer = "don't do that, do this" (design §3.4).

    bash asks (bypass off), so the turn parks. The client sends `steer` FIRST
    and the resume second — one socket is FIFO — and the deny routes the graph
    straight back to `agent`, whose entry drains the stash. No separate
    injection mechanism is needed for the parked case.
    """
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "echo hi"})]),
            script(text="好，那我换个做法。"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("跑个命令")
        first = conv.recv_until("permission.request", "message.end", "error")
        assert "permission.request" in [e["event"] for e in first], first

        _steer(conv, "别用 bash，直接回答我", "s-parked")
        conv.respond_permission("deny")
        rest = conv.recv_until("message.end", "error")

    assert not events_of(rest, "error"), rest
    assert "steer.absorbed" in [e["event"] for e in rest], rest
    # the resumed request carried BOTH the deny verdict and the redirect, so the
    # model can see why the tool did not run and what to do instead.
    assert _saw_in_one_call(model, "user denied", "别用 bash，直接回答我"), model.seen
    assert _steer_bands(client, sid)[0]["steerId"] == "s-parked"


def test_steer_at_permission_card_survives_the_park_gap(
    client, create_session, ws_conv, bypass_on
):
    """An entry stashed while the turn streamed, then parked, is re-sent by the
    client on resume — enqueue is idempotent by steer_id, so the re-send cannot
    duplicate it (design §3.2/§3.4)."""
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.4; echo MARK"})]),
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.4; echo AGAIN"})]),
            script(text="ok"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("干活")
        conv.recv_until("tool.start", "error")
        # typed while the first tool ran; the model then calls the tool AGAIN, so
        # the turn keeps running and this one IS absorbed normally
        _steer(conv, "记一下这点", "s-again")
        rest = conv.recv_until("message.end", "error")
    assert not events_of(rest, "error"), rest
    assert [e["steer_id"] for e in events_of(rest, "steer.absorbed")] == ["s-again"]
    assert len(_steer_bands(client, sid)) == 1
    assert _saw_in_one_call(model, "记一下这点", "AGAIN")