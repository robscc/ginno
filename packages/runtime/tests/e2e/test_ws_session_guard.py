"""Guard tests for ``api/stream/ws.py::session_ws`` — refactor batch 4-1.

批次 4-2 要把 session_ws()（~1400 行）的消息泵拆出去。动刀前这里是三
个命门面的行为冻结基线（批次 0 思路在 ws 层的重演）——每一条都对应
拆泵时最容易断的传递链：

① 断连补发窗。前端 ChatStream 的重连窗口是 60s（deadline = now+60_000，
   2026-11-09 起从 6s 放宽）；服务端在这窗口内可恢复的三样东西是：
   parked interrupt 重放（连接 open 段从 checkpointer 读）、``turn_state``
   probe、steer 队列（进程内 dict，跨 socket 存活）。已有覆盖：
   permission 重放（test_ws_reconnect_resume）、user_question 重放
   （test_ask_user_flow）。本文件补第三种 kind——version_propose——以及
   steer 队列的断线重发幂等（design §3.3：客户端断线重发是常态设计）。

② steer 路由。既有覆盖（test_steering_ws + test_steering）很厚：吸收/
   顺序/幂等/turn_id fallback。本文件补两条拆泵命门：多会话并行时
   steer 不串会话（队列按 session_id 分发，拆泵闭包捕获错 session_id
   即红）；客户端显式 turn_id 的回显契约（ack 归位链）。

③ 握手鉴权。OriginGuardMiddleware 本体已有 mini-app 决策表
   （test_origin_guard）；这里钉的是**主 server 路由树**上的 wiring
   行为——真实 session_ws() 握手：恶意 Origin 拒绝、tauri/loopback/
   无 Origin 放行。（环回 token 鉴权在 origin_guard.py 文档里明确标注
   为 follow-up，产品码尚未实施，不在本基线内。）
"""

from __future__ import annotations

import time

import pytest
from conftest import events_of, script, script_tool_call

from ginno_runtime import jsonx as json
from ginno_runtime import server, server_shared
from ginno_runtime.workflows import store as wf_store
from ginno_runtime.testing.fake_model import ScriptedChatModel
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# shared helpers (same idioms as test_steering_ws / test_ws_reconnect_resume)
# --------------------------------------------------------------------------- #
@pytest.fixture
def bypass_on(isolated_home):
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(json.dumps(s))


class RecordingScriptedChatModel(ScriptedChatModel):
    """Records every prompt so tests can assert what the MODEL actually saw."""

    seen: list = []

    def _next(self, messages=None):
        self.seen = [*self.seen, list(messages or [])]
        return super()._next(messages)


def _calls_containing(model: RecordingScriptedChatModel, needle: str) -> int:
    return sum(
        1 for call in model.seen if any(needle in str(getattr(m, "content", "")) for m in call)
    )


def _steer(conv, text: str, steer_id: str, turn_id: str | None = None) -> None:
    payload = {"type": "steer", "steer_id": steer_id, "message": text}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    conv.send(payload)


def _steer_bands(client, sid: str) -> list[dict]:
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    return [b for m in msgs for b in m.get("blocks", []) if b.get("kind") == "steer"]


def _await_parked_kind(sid: str, kind: str, timeout: float = 10.0) -> bool:
    """Poll the checkpointer until an interrupt of ``kind`` is durable — the
    same route the reconnect open-handler reads (graph.aget_state misses
    tools-node interrupts; see FileCheckpointer.get_pending_interrupt)."""
    deadline = time.time() + timeout
    sess = server_shared._SESSIONS[sid]
    cfg = {"configurable": {"thread_id": sid, "project_slug": "default"}}
    while time.time() < deadline:
        pending = sess["graph"].checkpointer.get_pending_interrupt(cfg)
        kinds = [(getattr(i, "value", None) or {}).get("kind") for i in pending or []]
        if kind in kinds:
            return True
        time.sleep(0.1)
    return False


# --------------------------------------------------------------------------- #
# ① 断连补发窗 — parked version_propose 重放（第三种 interrupt kind）
# --------------------------------------------------------------------------- #
def test_reconnect_reemits_a_parked_version_propose(client, create_session, ws_conv):
    """A turn parked at a workflow diff proposal survives the client socket
    dropping: a reconnecting socket gets the ``version.propose`` frame again
    (payload contract: workflow_id / from_version / diff / rationale), and the
    decision sent on the NEW socket resumes the turn to completion.

    This is the version_propose twin of
    test_ws_reconnect_resume.test_reconnect_reemits_a_parked_permission /
    test_ask_user_flow.test_reconnect_reemits_the_parked_question — the three
    interrupt kinds share one replay loop in session_ws()'s open section, and
    that loop is exactly what batch 4-2 lifts out of the giant function.
    """
    wf = wf_store.create_def(
        {
            "name": "GuardWF",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "a"},
                    {"id": "s2", "type": "step", "agent": "dev", "goal": "b"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            },
        }
    )
    wid = wf["id"]
    new_dsl = json.loads(json.dumps(wf["dsl"]))
    new_dsl["nodes"].append({"id": "s3", "type": "step", "agent": "dev", "goal": "guard"})
    new_dsl["edges"].append({"from": "s2", "to": "s3"})

    model = [
        script(
            text="",
            tool_calls=[
                script_tool_call(
                    "workflow_propose_edit",
                    {
                        "workflow_id": wid,
                        "new_dsl_json": json.dumps(new_dsl),
                        "rationale": "add s3",
                    },
                )
            ],
        ),
        script(text="done"),
    ]
    sid = create_session(model, agent_id="workflow-dev")

    with ws_conv(sid) as conv1:
        conv1.invoke("add a final step")
        first = conv1.recv_until("version.propose", "message.end", "error")
        assert events_of(first, "version.propose"), [e.get("event") for e in first]
        assert _await_parked_kind(sid, "version_propose")

    # The client dropped. The reconnect window is 60s on the frontend; the
    # server-side contract is that a fresh socket sees the parked proposal
    # again, unprompted, as its opening frames.
    with ws_conv(sid) as conv2:
        again = conv2.recv_until("version.propose", "error", "message.end")
        assert not events_of(again, "error"), again
        prop = events_of(again, "version.propose")[0]
        # payload contract (mirrors engine.py's emit):
        assert prop["workflow_id"] == wid
        assert prop["from_version"] == 1
        assert "s3" in str(prop["diff"])
        assert prop["rationale"] == "add s3"
        # The replay is a DIRECT frame (ws.send_text in the open section),
        # not a broadcast: it carries no frame_session. The frontend's frame
        # ownership gate treats a missing frame_session as pass
        # (useChatStreamEngine: `if (ev.frame_session && ...)`), and ownership
        # is implied by the connection itself. Batch 4-2 must keep the replay
        # reaching the reconnecting socket; switching it to the broadcast
        # path would ADD frame_session (fine) but must not drop it.
        assert "frame_session" not in prop or prop["frame_session"] == sid
        # the graph is genuinely paused (no terminal before the decision)
        assert "message.end" not in [e.get("event") for e in again]

        # the decision rides the new socket and the turn completes
        conv2.send({"type": "permission_response", "decision": "allow"})
        rest = conv2.recv_until("message.end", "error")
        assert not events_of(rest, "error"), rest

    assert wf_store.get_def(wid)["version"] == 2
    assert len(wf_store.get_def(wid)["dsl"]["nodes"]) == 3


def test_steer_resend_after_reconnect_is_absorbed_once(client, create_session, ws_conv, bypass_on):
    """A steer queued before a disconnect is NOT lost, and the client's
    resend of the SAME steer_id (the documented reconnect protocol,
    steering-design §3.3) does not double-inject: the queue dedups by
    steer_id, the resend still earns an ack, and exactly ONE absorption
    happens.

    KNOWN DEFECT FROZEN HERE (reported, product code untouched per batch 4-1
    constraints): ``server_shared.steer_merge_pending`` matches on
    text-similarity WITHOUT comparing steer_id, so a same-id resend hits the
    merge path and the queued entry's text becomes the two copies joined by
    a blank line ("X\\n\\nX"). The band/model text assertions below pin that
    current output on purpose — batch 4-2 must not make it WORSE (no second
    band, no second absorption, no unbounded copies); fixing the merge
    (dedup on steer_id) will need this test updated alongside."""
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.8; echo MARK"})]),
            script(text="done"),
        ]
    )
    sid = create_session(model, agent_id="dev")

    with ws_conv(sid) as conv1:
        conv1.invoke("干活")
        conv1.recv_until("tool.start", "error")
        _steer(conv1, "断线前的注入", "steer-r")
        # ack for the original send (broadcast; the queue now holds it)
        acked = conv1.recv_until("steer.accepted", "error")
        assert events_of(acked, "steer.accepted")[0]["steer_id"] == "steer-r"

    # client dropped and reconnected: probe first (frontend protocol), then
    # the resend with the SAME steer_id. The turn itself survived the socket
    # drop (background task, not tied to the handler).
    with ws_conv(sid) as conv2:
        conv2.send({"type": "turn_state"})
        state = conv2.recv_until("turn.state", "error")[-1]
        assert state["event"] == "turn.state" and state["running"] is True

        _steer(conv2, "断线前的注入", "steer-r")
        rest = conv2.recv_until("message.end", "error")

    assert not events_of(rest, "error"), rest
    # exactly one absorption for the (resent, same-id) steer — the queue
    # entry is injected once, not once per send
    absorbed = events_of(rest, "steer.absorbed")
    assert [e["steer_id"] for e in absorbed] == ["steer-r"], absorbed
    # the resend still earns its ack (the client's queue entry closes)
    assert events_of(rest, "steer.accepted"), rest
    # the steered text reaches the model inside the same turn (not a
    # next-turn queue) — currently as the merged (duplicated) text, see the
    # frozen-defect note above
    assert _calls_containing(model, "断线前的注入") == 1, model.seen
    # transcript: exactly ONE band (no second band from the resend); its text
    # is the current merged output — frozen, see the note above
    bands = _steer_bands(client, sid)
    assert [b["steerId"] for b in bands] == ["steer-r"], bands
    assert bands[0]["text"] == "断线前的注入\n\n断线前的注入", bands[0]["text"]


# --------------------------------------------------------------------------- #
# ② steer 路由 — 并行会话不串 / 显式 turn_id 回显
# --------------------------------------------------------------------------- #
def test_parallel_sessions_steers_do_not_cross(client, create_session, ws_conv, bypass_on):
    """Two sessions run slow turns side by side; each gets its own steer.
    The steer must stay inside its session: the OTHER session's model never
    sees the text, and each ack is frame-stamped for its own session. This is
    the routing invariant a message-pump extraction is most likely to break
    (a loop-variable capture or shared queue would cross-wire them)."""
    model_a = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.8; echo A"})]),
            script(text="A done"),
        ]
    )
    model_b = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.8; echo B"})]),
            script(text="B done"),
        ]
    )
    sid_a = create_session(model_a, agent_id="dev")
    sid_b = create_session(model_b, agent_id="dev")

    with ws_conv(sid_a) as conv_a, ws_conv(sid_b) as conv_b:
        conv_a.invoke("A 干活")
        conv_a.recv_until("tool.start", "error")
        conv_b.invoke("B 干活")
        conv_b.recv_until("tool.start", "error")

        _steer(conv_a, "α-只属于A会话", "steer-a")
        _steer(conv_b, "β-只属于B会话", "steer-b")

        rest_a = conv_a.recv_until("message.end", "error")
        rest_b = conv_b.recv_until("message.end", "error")

    assert not events_of(rest_a, "error"), rest_a
    assert not events_of(rest_b, "error"), rest_b
    # each session absorbed exactly its own steer
    assert [e["steer_id"] for e in events_of(rest_a, "steer.absorbed")] == ["steer-a"]
    assert [e["steer_id"] for e in events_of(rest_b, "steer.absorbed")] == ["steer-b"]
    # ack frames are session-stamped (frame ownership, never cross-wired)
    for e in events_of(rest_a, "steer.accepted"):
        assert e.get("frame_session") == sid_a, e
    for e in events_of(rest_b, "steer.accepted"):
        assert e.get("frame_session") == sid_b, e
    # the MODELS saw only their own steer text
    assert _calls_containing(model_a, "α-只属于A会话") == 1, model_a.seen
    assert _calls_containing(model_a, "β-只属于B会话") == 0, model_a.seen
    assert _calls_containing(model_b, "β-只属于B会话") == 1, model_b.seen
    assert _calls_containing(model_b, "α-只属于A会话") == 0, model_b.seen
    # transcripts stay per-session too
    assert [b["steerId"] for b in _steer_bands(client, sid_a)] == ["steer-a"]
    assert [b["steerId"] for b in _steer_bands(client, sid_b)] == ["steer-b"]


def test_steer_accept_echoes_the_client_supplied_turn_id(
    client, create_session, ws_conv, bypass_on
):
    """The ack chain echoes the client-supplied turn_id: ``steer.accepted``
    and ``steer.absorbed`` both carry the turn the client named, which is how
    the frontend files the ack onto the right bubble. (The no-turn_id fallback
    is already pinned by test_steer_needs_no_turn_id_from_the_client; this
    freezes the explicit-id path.)"""
    model = RecordingScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "sleep 0.6; echo MARK"})]),
            script(text="ok"),
        ]
    )
    sid = create_session(model, agent_id="dev")

    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "干活", "turn_id": "turn-explicit-1"})
        conv.recv_until("tool.start", "error")
        _steer(conv, "带显式turn_id的注入", "steer-t", turn_id="turn-explicit-1")
        rest = conv.recv_until("message.end", "error")

    assert not events_of(rest, "error"), rest
    ack = events_of(rest, "steer.accepted")[0]
    assert ack["steer_id"] == "steer-t"
    assert ack.get("turn_id") == "turn-explicit-1", ack
    ab = events_of(rest, "steer.absorbed")[0]
    assert ab["steer_id"] == "steer-t"
    assert ab.get("turn_id") == "turn-explicit-1", ab


# --------------------------------------------------------------------------- #
# ③ 握手鉴权 — 主 server 路由树上的 Origin 决策（wiring 行为）
# --------------------------------------------------------------------------- #
def test_main_server_ws_handshake_rejects_evil_origin(client, create_session):
    """The real session_ws() handshake, behind the real middleware stack:
    a hostile page's WS upgrade must be refused before accept (the e2e
    WS-403 incident class). No WebSocket is ever established."""
    sid = create_session([script(text="hi")], agent_id="dev")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/ws/sessions/{sid}",
            headers={"Origin": "https://evil.example"},
        ):
            pass


def test_main_server_ws_handshake_accepts_known_origins(client, create_session):
    """The packaged webview (tauri://localhost), the dev server
    (http://localhost:3000) and a same-origin loopback port all complete the
    handshake against the REAL session route and can run the turn_state
    probe."""
    sid = create_session([script(text="hi")], agent_id="dev")
    for origin in (
        "tauri://localhost",
        "https://tauri.localhost",
        "http://localhost:3000",
        "http://127.0.0.1:8787",
    ):
        with client.websocket_connect(f"/api/ws/sessions/{sid}", headers={"Origin": origin}) as ws:
            ws.send_json({"type": "turn_state"})
            state = ws.receive_json()
        assert state["event"] == "turn.state", (origin, state)
        assert state["running"] is False, (origin, state)


def test_main_server_ws_handshake_accepts_no_origin(client, create_session):
    """Non-browser clients (curl / harness / rtk) send no Origin at all and
    stay allowed — pinned explicitly at the real route."""
    sid = create_session([script(text="hi")], agent_id="dev")
    with client.websocket_connect(f"/api/ws/sessions/{sid}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["event"] == "pong"
