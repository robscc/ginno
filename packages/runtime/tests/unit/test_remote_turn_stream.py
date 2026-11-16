"""远端 turn 流式消费（2026-11-17）：client.stream_message + 执行器转发。

两层测试：
1. client 层——httpx MockTransport 全链路 SSE（零真网络）：帧解析、终态去重、
   建流失败 / 中途断流 / 空闲超时的错误规整、context_id/reference_task_ids 透传。
2. 执行器层——**真跑 run_remote_turn**（只在 _client seam 注入假 client，
   绝不桩执行器主体；假绿教训见 test_remote_session.py 尾部注释）：
   - ⚠️ 关键：frame_session 必须改写为本地 sid（用**真 _push_session_event**
     + 假 socket 钉住线上实际字节，不是桩捕获）；
   - tool/token/thinking/usage 事件转发；终稿不重复灌全文；
   - contextId 第二轮复用 + input-required 的 referenceTaskIds（H2）；
   - 建流失败回落阻塞 send_message；中途断流回落轮询（不重复派活）；
   - 失败路断言区分「对端故障」与「自身 bug」（TypeError 不得伪装成网络错误）。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import server_shared as shared
from ginno_runtime.a2a import card
from ginno_runtime.a2a.address import fingerprint_of
from ginno_runtime.a2a.client import A2AClient
from ginno_runtime.a2a.errors import A2AError
from ginno_runtime.api.stream import remote_turn as rt

pytestmark = pytest.mark.unit

ENDPOINT = "https://peer.example.com/a2a/CAP"
REMOTE_SID = "remote-sess-999"  # 远端机器上的会话 id（与本地不同！）
REMOTE_TID = "remote-turn-777"


# --------------------------------------------------------------------------- #
# 帧构造助手（形状对齐 a2a/server._sse_task_stream + 节点侧事件汇记录）
# --------------------------------------------------------------------------- #
def _status_data(state, task_id="task-1", ctx="ctx-remote-1", text=None):
    d = {"id": task_id, "contextId": ctx, "status": {"state": state}}
    if text is not None:
        d["status"]["message"] = {
            "role": "agent", "parts": [{"kind": "text", "text": text}],
        }
    return d


def _status_frame(state, **kw):
    return {"event": "status", "data": _status_data(state, **kw)}


def _turn_event_frame(event, data, task_id="task-1"):
    """节点侧事件汇的 progress 记录：data 里带远端引擎盖的 frame_session/turn_id。"""
    stamped = {"frame_session": REMOTE_SID, "turn_id": REMOTE_TID, **data}
    return {
        "event": "progress",
        "data": {"taskId": task_id, "ts": 1763300000.0,
                 "kind": "turn_event", "event": event, "data": stamped},
    }


def _sse_body(frames) -> bytes:
    """把帧列表编成 SSE 字节流（与 server._sse_frame 同形）。"""
    out = []
    for f in frames:
        out.append(f"event: {f['event']}\ndata: {json.dumps(f['data'])}\n\n")
    return "".join(out).encode("utf-8")


def _sse_client(frames=None, *, status=200, content_type="text/event-stream",
                body=None, handler=None, **kw) -> A2AClient:
    if handler is None:
        payload = body if body is not None else _sse_body(frames or [])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, headers={"content-type": content_type},
                                  content=payload)

    return A2AClient(
        ENDPOINT, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw
    )


async def _collect(gen) -> list[dict]:
    return [f async for f in gen]


# --------------------------------------------------------------------------- #
# 1. client.stream_message —— SSE 消费
# --------------------------------------------------------------------------- #
async def test_stream_message_rpc_shape_url_and_ids_passthrough():
    """POST endpoint/message/stream；contextId/referenceTaskIds 透传（多轮续作）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse_body([_status_frame("completed", text="ok")]),
        )

    c = _sse_client(handler=handler, token="tk-stream")
    await _collect(c.stream_message("干活", context_id="c-9", reference_task_ids=["t-old"]))
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == ENDPOINT + "/message/stream"
    assert req.headers["Accept"] == "text/event-stream"
    assert req.headers["Authorization"] == "Bearer tk-stream"
    body = json.loads(req.content)
    assert body["jsonrpc"] == "2.0" and body["method"] == "message/stream"
    msg = body["params"]["message"]
    assert msg["contextId"] == "c-9"
    assert msg["referenceTaskIds"] == ["t-old"]
    assert msg["parts"] == [{"kind": "text", "text": "干活"}]
    assert msg["messageId"]


async def test_stream_message_yields_status_and_progress_in_order():
    frames = [
        _status_frame("working"),
        _turn_event_frame("tool.start", {"name": "bash", "id": "tc1"}),
        _turn_event_frame("token.delta", {"content": "你好"}),
        _status_frame("completed", text="你好"),
    ]
    got = await _collect(_sse_client(frames).stream_message("hi"))
    assert [f["event"] for f in got] == ["status", "progress", "progress", "status"]
    assert got[1]["data"]["kind"] == "turn_event"
    assert got[1]["data"]["event"] == "tool.start"
    assert got[1]["data"]["taskId"] == "task-1"
    assert got[2]["data"]["data"]["content"] == "你好"
    assert got[3]["data"]["status"]["state"] == "completed"


async def test_stream_message_terminal_frame_dedup_and_stop():
    """终态帧只放行一次；其后的重复终态/杂帧一律不再流出（防御对端补发）。"""
    frames = [
        _status_frame("working"),
        _status_frame("completed", text="done"),
        _status_frame("completed", text="done"),  # 重复终态
        _turn_event_frame("token.delta", {"content": "late"}),  # 终态后杂帧
    ]
    got = await _collect(_sse_client(frames).stream_message("hi"))
    states = [f["data"]["status"]["state"] for f in got if f["event"] == "status"]
    assert states == ["working", "completed"], f"终态帧重复或未收流: {states}"
    assert len(got) == 2


async def test_stream_message_input_required_ends_stream():
    """中断态（input-required）同样收流——对端等调用方动作，流不会自己前进。"""
    frames = [_status_frame("working"), _status_frame("input-required"),
              _status_frame("input-required")]
    got = await _collect(_sse_client(frames).stream_message("hi"))
    assert [f["data"]["status"]["state"] for f in got] == ["working", "input-required"]


async def test_stream_message_sse_comments_multiline_data_and_id_fields():
    """SSE 规范细节：注释行忽略、多行 data 拼接、id/retry 字段忽略。"""
    pretty = json.dumps(_status_data("completed", text="ok"), indent=2)
    data_lines = "\n".join("data: " + ln for ln in pretty.splitlines())
    body = (
        ": keepalive probe\n\n"
        "id: 42\nretry: 1000\nevent: status\n" + data_lines + "\n\n"
    ).encode()
    got = await _collect(_sse_client(body=body).stream_message("hi"))
    assert len(got) == 1
    assert got[0]["event"] == "status"
    assert got[0]["data"]["status"]["state"] == "completed"


async def test_stream_message_malformed_frame_skipped_not_fatal():
    body = (
        b"event: progress\ndata: {broken json\n\n"
        + _sse_body([_status_frame("completed", text="ok")])
    )
    got = await _collect(_sse_client(body=body).stream_message("hi"))
    assert len(got) == 1 and got[0]["data"]["status"]["state"] == "completed"


async def test_stream_message_http_error_raises_a2a_error():
    c = _sse_client(status=404, content_type="application/json",
                    body=b'{"detail":"Not Found"}')
    with pytest.raises(A2AError, match="404"):
        await _collect(c.stream_message("hi"))


async def test_stream_message_rpc_error_in_json_response_mapped():
    """旧节点不支持 message/stream：200 + JSON-RPC error → 语义码保留。"""
    err = {"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"}}
    c = _sse_client(content_type="application/json", body=json.dumps(err).encode())
    with pytest.raises(A2AError) as ei:
        await _collect(c.stream_message("hi"))
    assert ei.value.code == -32601


async def test_stream_message_json_task_response_yields_single_status():
    """对端受理了任务但以 JSON（非 SSE）回应 → 单帧 status，调用方转轮询。

    绝不抛错触发 send_message 回落——那会重复派活。
    """
    resp = {"jsonrpc": "2.0", "id": 1, "result": _status_data("working")}
    c = _sse_client(content_type="application/json", body=json.dumps(resp).encode())
    got = await _collect(c.stream_message("hi"))
    assert len(got) == 1
    assert got[0]["event"] == "status" and got[0]["data"]["id"] == "task-1"


class _ScriptedStream(httpx.AsyncByteStream):
    """按脚本吐字节，然后抛指定异常（模拟中途断流 / 空闲超时）。"""

    def __init__(self, chunks: list[bytes], exc: Exception | None):
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self):
        for c in self._chunks:
            yield c
        if self._exc is not None:
            raise self._exc


async def test_stream_message_mid_stream_disconnect_raises_after_partial_frames():
    head = _sse_body([_status_frame("working"),
                      _turn_event_frame("token.delta", {"content": "部分"})])
    stream = _ScriptedStream([head], httpx.ReadError("connection reset"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=stream)

    got: list[dict] = []
    with pytest.raises(A2AError, match="流"):
        async for f in _sse_client(handler=handler).stream_message("hi"):
            got.append(f)
    assert len(got) == 2, "断流前已到达的帧必须照常交付"
    assert got[0]["data"]["status"]["state"] == "working"


async def test_stream_message_idle_timeout_wrapped():
    stream = _ScriptedStream([_sse_body([_status_frame("working")])],
                             httpx.ReadTimeout("idle too long"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=stream)

    with pytest.raises(A2AError):
        await _collect(_sse_client(handler=handler).stream_message("hi"))


async def test_stream_message_connect_error_wrapped():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(A2AError, match="网络错误"):
        await _collect(_sse_client(handler=boom).stream_message("hi"))


async def test_stream_message_clean_eof_without_terminal_returns():
    """对端空闲收流（未达终态）→ 正常结束，由调用方决定轮询，不抛错。"""
    got = await _collect(_sse_client([_status_frame("working")]).stream_message("hi"))
    assert len(got) == 1


async def test_stream_message_fp_pinning_checked_before_request(monkeypatch):
    """指纹 pinning 与 _rpc 同口径：校验失败一个字节都不发。"""
    der = b"\x30\x82\x01\x0a-fake-peer-cert"
    monkeypatch.setattr(card, "_peer_cert_der", lambda h, p, t: der)
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_body([_status_frame("completed")]))

    c = _sse_client(handler=handler, verify_fp=fingerprint_of(b"impostor"))
    with pytest.raises(A2AError, match="指纹不匹配"):
        await _collect(c.stream_message("hi"))
    assert seen == []


# --------------------------------------------------------------------------- #
# 2. 执行器层——真跑 run_remote_turn
# --------------------------------------------------------------------------- #
def _fake_task(state, text=None, task_id="task-1", ctx="ctx-remote-1"):
    """轮询/get_task 返回的 task（SimpleNamespace 形状，_task_text 鸭子类型）。"""
    msg = None
    if text is not None:
        msg = SimpleNamespace(parts=[SimpleNamespace(text=text)])
    return SimpleNamespace(
        id=task_id, context_id=ctx,
        status=SimpleNamespace(state=state, message=msg), artifacts=[],
    )


class FakeStreamClient:
    """只在 _client seam 注入——执行器主体真跑。

    frames 逐帧 yield；stream_error 在帧吐完后抛（建流失败 = 空帧 + 异常）。
    """

    def __init__(self, *, frames=None, stream_error=None,
                 send_result=None, send_error=None, poll_results=None):
        self.frames = list(frames or [])
        self.stream_error = stream_error
        self.send_result = send_result
        self.send_error = send_error
        self.poll_results = list(poll_results or [])
        self.calls: list[dict] = []
        self.polls: list[str] = []
        self.cancelled: list[str] = []

    async def stream_message(self, text, *, context_id=None, reference_task_ids=None):
        self.calls.append({"via": "stream", "text": text,
                           "context_id": context_id, "refs": reference_task_ids})
        for f in self.frames:
            yield f
        if self.stream_error is not None:
            raise self.stream_error

    async def send_message(self, text, *, context_id=None, reference_task_ids=None):
        self.calls.append({"via": "send", "text": text,
                           "context_id": context_id, "refs": reference_task_ids})
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    async def get_task(self, task_id):
        self.polls.append(task_id)
        if self.poll_results:
            return self.poll_results.pop(0)
        return _fake_task("completed", text="poll-done")

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return {}

    async def aclose(self):
        pass


@pytest.fixture
def wire(monkeypatch, isolated_home):
    """真 ContextMap（隔离 home）+ 假 push 捕获 + 可注入的假 client。

    **不桩 run_remote_turn**；_POLL_S 压到 10ms 让轮询兜底测试不真等 3s。
    """
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="http://127.0.0.1:18790/a2a/CAP")
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    monkeypatch.setattr(rt, "_POLL_S", 0.01)
    events: list = []

    async def fake_push(sid, ev, data, tid=None):
        events.append((sid, ev, data, tid))

    monkeypatch.setattr(rt, "_push_session_event", fake_push)
    persisted: list = []

    async def fake_persist(session, user_text, reply):
        persisted.append(reply)

    monkeypatch.setattr(rt, "_persist_exchange", fake_persist)
    clients: list = []

    def install(client):
        clients.append(client)
        monkeypatch.setattr(rt, "_client", lambda rec: clients.pop(0))
        return client

    return SimpleNamespace(events=events, persisted=persisted, install=install,
                           clients=clients)


def _evs(events, name):
    return [d for _, ev, d, _ in events if ev == name]


async def test_stream_forwards_tool_thinking_usage_events(wire):
    """tool/token/thinking/usage 事件全部转发，内容原样（除归属键）。"""
    frames = [
        _status_frame("working"),
        _turn_event_frame("thinking.delta", {"content": "让我想想"}),
        _turn_event_frame("tool.start", {"name": "bash", "id": "tc1"}),
        _turn_event_frame("tool.input", {"id": "tc1", "input": {"command": "ls"}}),
        _turn_event_frame("tool.end", {"id": "tc1", "content": "file-a"}),
        _turn_event_frame("usage", {"turn": {"input_tokens": 10, "output_tokens": 5}}),
        _turn_event_frame("token.delta", {"content": "结果是 file-a"}),
        _status_frame("completed", text="结果是 file-a"),
    ]
    wire.install(FakeStreamClient(frames=frames))
    sess = {"id": "s-fwd", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "列文件", "tid-fwd")
    assert res["ok"] is True, f"流式成功路必须通：{res.get('error')!r}"
    names = [ev for _, ev, _, _ in wire.events]
    for expect in ("thinking.delta", "tool.start", "tool.input", "tool.end",
                   "usage", "token.delta"):
        assert expect in names, f"缺事件 {expect}"
    assert _evs(wire.events, "tool.start")[0]["name"] == "bash"
    assert _evs(wire.events, "tool.end")[0]["content"] == "file-a"
    assert _evs(wire.events, "usage")[0]["turn"]["input_tokens"] == 10
    # 接单 notice 保留且只发一次；working→completed 状态 notice 保留
    notices = _evs(wire.events, "notice")
    assert sum(1 for d in notices if "已接单" in d.get("message", "")) == 1
    assert any("远端状态：completed" in d.get("message", "") for d in notices)
    assert not any(d.get("kind") == "error" for d in notices)
    assert wire.persisted == ["结果是 file-a"]


async def test_stream_frame_session_rewritten_to_local_sid(wire, monkeypatch):
    """⚠️ 关键契约：远端帧的 frame_session 必须改写为本地 sid。

    用**真 _push_session_event + 假 socket**钉住线上实际字节——前端守卫
    （useChatStreamEngine.ts）丢弃 frame_session ≠ 本 socket 会话 id 的帧，
    不改写则功能表面实现、实际一帧都不渲染。
    """
    frames = [
        _status_frame("working"),
        _turn_event_frame("tool.start", {"name": "bash", "id": "tc1"}),
        _turn_event_frame("tool.end", {"id": "tc1", "content": "ok"}),
        _turn_event_frame("token.delta", {"content": "你好"}),
        _status_frame("completed", text="你好"),
    ]
    wire.install(FakeStreamClient(frames=frames))
    sid = "local-sess-1"
    sent: list[dict] = []

    class FakeWS:
        async def send_text(self, data):
            sent.append(json.loads(data))

    # 恢复真 _push_session_event（wire fixture 桩了它）——走真实盖章/广播路径
    monkeypatch.setattr(rt, "_push_session_event", shared._push_session_event)
    monkeypatch.setattr(shared, "_SESSION_WS", {sid: [FakeWS()]})
    sess = {"id": sid, "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-local")
    assert res["ok"] is True, res.get("error")
    assert sent, "真 socket 必须收到帧"
    for f in sent:
        assert f.get("frame_session") == sid, (
            f"frame_session 未改写为本地 sid，前端会丢弃：{f}"
        )
    blob = json.dumps(sent)
    assert REMOTE_SID not in blob, "远端会话 id 不得泄漏到本地帧"
    assert REMOTE_TID not in blob, "远端 turn_id 不得泄漏到本地帧"
    tool_starts = [f for f in sent if f["event"] == "tool.start"]
    assert tool_starts and tool_starts[0]["name"] == "bash"
    assert tool_starts[0]["turn_id"] == "tid-local"
    assert any(f["event"] == "tool.end" for f in sent)
    assert any(f["event"] == "token.delta" for f in sent)


async def test_stream_token_delta_not_replayed_as_full_text(wire):
    """流式已推过 token.delta → 终稿绝不重复灌全文（会重复渲染）。"""
    frames = [
        _status_frame("working"),
        _turn_event_frame("token.delta", {"content": "你好"}),
        _turn_event_frame("token.delta", {"content": "，世界"}),
        _status_frame("completed", text="你好，世界"),
    ]
    wire.install(FakeStreamClient(frames=frames))
    sess = {"id": "s-noreplay", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-nr")
    assert res["ok"] is True, res.get("error")
    deltas = [d["content"] for d in _evs(wire.events, "token.delta")]
    assert deltas == ["你好", "，世界"], f"终稿被重复灌全文或片段丢失: {deltas}"
    assert res["text"] == "你好，世界"
    assert wire.persisted == ["你好，世界"], "历史仍须落终稿全文"


async def test_stream_without_deltas_still_bulk_pushes_final_text(wire):
    """旧节点无事件汇（只有 status 帧）→ 保留一次性灌全文的既有行为。"""
    frames = [_status_frame("working"), _status_frame("completed", text="最终答复")]
    wire.install(FakeStreamClient(frames=frames))
    sess = {"id": "s-bulk", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-bulk")
    assert res["ok"] is True, res.get("error")
    deltas = [d["content"] for d in _evs(wire.events, "token.delta")]
    assert deltas == ["最终答复"]


async def test_stream_deltas_without_terminal_message_uses_streamed_text(wire):
    """终态 task 无 status.message（回复只经 delta 下发）→ 用已流式内容兜底。"""
    frames = [
        _status_frame("working"),
        _turn_event_frame("token.delta", {"content": "纯流式回复"}),
        _status_frame("completed"),  # 无 message
    ]
    wire.install(FakeStreamClient(frames=frames))
    sess = {"id": "s-deltaonly", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-do")
    assert res["ok"] is True, res.get("error")
    assert res["text"] == "纯流式回复"
    assert wire.persisted == ["纯流式回复"]
    assert [d["content"] for d in _evs(wire.events, "token.delta")] == ["纯流式回复"]


async def test_stream_second_turn_reuses_context_id(wire):
    """第二轮必须带上首轮 status 帧里的 contextId（云端续作基础）。"""
    c1 = FakeStreamClient(frames=[_status_frame("completed", text="一轮")])
    c2 = FakeStreamClient(frames=[_status_frame("completed", text="二轮")])
    wire.install(c1)
    sess = {"id": "s-ctx", "remote_peer": "vps", "graph": None}
    r1 = await rt.run_remote_turn(sess, "第一轮", "t1")
    assert r1["ok"] is True, r1.get("error")
    wire.install(c2)
    r2 = await rt.run_remote_turn(sess, "第二轮", "t2")
    assert r2["ok"] is True, r2.get("error")
    assert c2.calls[0]["context_id"] == "ctx-remote-1"


async def test_stream_input_required_next_turn_references_task(wire):
    """H2：上轮 input-required 挂起 → 本轮 referenceTaskIds 指向该 task。"""
    c1 = FakeStreamClient(frames=[_status_frame("input-required")])
    c2 = FakeStreamClient(frames=[_status_frame("completed", text="续作完成")])
    wire.install(c1)
    sess = {"id": "s-ref", "remote_peer": "vps", "graph": None}
    r1 = await rt.run_remote_turn(sess, "第一轮", "t1")
    assert r1["ok"] is True, r1.get("error")
    assert r1["state"] == "input-required"
    wire.install(c2)
    r2 = await rt.run_remote_turn(sess, "批准", "t2")
    assert r2["ok"] is True, r2.get("error")
    assert c2.calls[0]["refs"] == ["task-1"]


async def test_stream_establishment_failure_falls_back_to_send(wire):
    """建流失败（对端不支持 streaming）→ 回落阻塞 send_message，远端仍可用。"""
    client = FakeStreamClient(
        frames=[], stream_error=A2AError("A2A 请求失败：HTTP 404"),
        send_result=_fake_task("completed", text="阻塞回落答复"),
    )
    wire.install(client)
    sess = {"id": "s-fallback", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-fb")
    assert res["ok"] is True, f"回落必须成功：{res.get('error')!r}"
    assert res["text"] == "阻塞回落答复"
    assert [c["via"] for c in client.calls] == ["stream", "send"]
    assert client.calls[1]["text"] == "hi"
    notices = _evs(wire.events, "notice")
    assert any("已接单" in d.get("message", "") for d in notices)
    assert not any(d.get("kind") == "error" for d in notices)
    assert [d["content"] for d in _evs(wire.events, "token.delta")] == ["阻塞回落答复"]


async def test_stream_mid_break_falls_back_to_polling_no_redispatch(wire):
    """中途断流：任务已在对端跑 → 轮询兜底，绝不重复派活；只补未推的文本尾部。"""
    client = FakeStreamClient(
        frames=[_status_frame("working"),
                _turn_event_frame("token.delta", {"content": "你好"})],
        stream_error=A2AError("A2A 流式请求失败（网络错误或流中断）：reset"),
        poll_results=[_fake_task("completed", text="你好，世界")],
    )
    wire.install(client)
    sess = {"id": "s-midbreak", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-mb")
    assert res["ok"] is True, f"断流回落必须成功：{res.get('error')!r}"
    assert [c["via"] for c in client.calls] == ["stream"], "不得重复派活"
    assert client.polls == ["task-1"]
    deltas = [d["content"] for d in _evs(wire.events, "token.delta")]
    assert deltas == ["你好", "，世界"], f"只补尾部，不重复灌全文: {deltas}"
    assert res["text"] == "你好，世界"
    assert wire.persisted == ["你好，世界"]


async def test_stream_clean_eof_nonterminal_polls_to_completion(wire):
    """流提前收（对端空闲超时 / submitted 占位）→ 轮询到终态。"""
    client = FakeStreamClient(
        frames=[_status_frame("submitted")],
        poll_results=[_fake_task("working"), _fake_task("completed", text="轮询答复")],
    )
    wire.install(client)
    sess = {"id": "s-eof", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-eof")
    assert res["ok"] is True, res.get("error")
    assert res["text"] == "轮询答复"
    assert len(client.polls) == 2
    notices = _evs(wire.events, "notice")
    assert any("远端状态：completed" in d.get("message", "") for d in notices)


async def test_stream_and_send_both_fail_reports_peer_error_not_own_bug(wire):
    """失败路必须区分对端故障与自身 bug：错误是 A2AError，不是 TypeError。"""
    client = FakeStreamClient(
        frames=[], stream_error=A2AError("A2A 请求失败：HTTP 502"),
        send_error=A2AError("A2A 请求失败（网络错误）：connection refused"),
    )
    wire.install(client)
    sess = {"id": "s-bothfail", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-bf")
    assert res["ok"] is False
    err = str(res.get("error") or "")
    assert "A2AError" in err, f"必须是对端故障（A2AError），实际：{err}"
    assert "TypeError" not in err and "AttributeError" not in err, (
        f"自身代码 bug 不得伪装成对端故障：{err}"
    )
    names = [ev for _, ev, _, _ in wire.events]
    assert "turn.start" in names and "done" in names, "失败也要发终态"
    assert any(d.get("kind") == "error" for d in _evs(wire.events, "notice"))
    assert wire.persisted and "远端执行失败" in wire.persisted[0]


async def test_stream_deadline_exceeded_fails_with_notice(wire, monkeypatch):
    """总时限（task_timeout_s）耗尽 → failed + error notice + done，不挂死。"""
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="http://h/a2a", task_timeout_s=-1)
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    client = FakeStreamClient(frames=[_status_frame("working")])
    wire.install(client)
    sess = {"id": "s-deadline", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-dl")
    assert res["ok"] is False
    assert res["state"] == "failed"
    assert "超时" in res["error"]
    assert "TypeError" not in res["error"]
    assert any(d.get("kind") == "error" for d in _evs(wire.events, "notice"))
    assert "done" in [ev for _, ev, _, _ in wire.events]


async def test_stream_lifecycle_events_suppressed(wire):
    """远端 turn.start/message.end/done 不转发——生命周期归本地执行器。"""
    frames = [
        _status_frame("working"),
        _turn_event_frame("turn.start", {"turn_id": REMOTE_TID, "name": "x"}),
        _turn_event_frame("token.delta", {"content": "正文"}),
        _turn_event_frame("message.end", {}),
        _turn_event_frame("done", {"state": "completed"}),
        _status_frame("completed", text="正文"),
    ]
    wire.install(FakeStreamClient(frames=frames))
    sess = {"id": "s-lifecycle", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-lc")
    assert res["ok"] is True, res.get("error")
    starts = _evs(wire.events, "turn.start")
    assert len(starts) == 1 and starts[0]["remote"] is True and starts[0]["peer"] == "vps"
    dones = _evs(wire.events, "done")
    assert len(dones) == 1 and dones[0]["state"] == "completed"
    assert len(_evs(wire.events, "message.end")) == 1


async def test_client_without_stream_capability_uses_blocking_path(wire):
    """注入的 client 无 stream_message（旧实现）→ 直接阻塞路径，不报错。"""

    class SendOnlyClient:
        def __init__(self):
            self.calls = []

        async def send_message(self, text, *, context_id=None, reference_task_ids=None):
            self.calls.append(text)
            return _fake_task("completed", text="旧式答复")

        async def aclose(self):
            pass

    client = SendOnlyClient()
    wire.install(client)
    sess = {"id": "s-sendonly", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "hi", "tid-so")
    assert res["ok"] is True, res.get("error")
    assert client.calls == ["hi"]
    assert [d["content"] for d in _evs(wire.events, "token.delta")] == ["旧式答复"]


# --------------------------------------------------------------------------- #
# 3. 集成——真 a2a server（build_a2a_app）× 真 A2AClient.stream_message
#    假执行器写进度 JSONL 的形状与节点侧事件汇完全一致（emit_progress 契约：
#    {ts, kind:"turn_event", event, data}），验证 SSE 解析对齐真实线上帧。
# --------------------------------------------------------------------------- #
def _integration_deps(tmp_path):
    from ginno_runtime.a2a import auth, server

    return server.A2ADeps(
        tokens=auth.TokenStore(tmp_path / "tokens.json"),
        audit=server.AuditLog(tmp_path / "audit.jsonl"),
        limiter=server.TokenBucketLimiter(30),
        agent_name="Ginno Node",
        agent_version="9.9.9",
    )


class _SinkLikeExecutor:
    """模拟节点侧入站执行：写 turn_event 进度记录（事件汇形状）后完成。"""

    def __init__(self, events):
        self._events = events

    async def run(self, *, instruction, context_id, peer_name, task_id):
        from ginno_runtime import paths
        from ginno_runtime.a2a.models import TaskState

        prog = paths.a2a_dir() / f"inbound-progress-{task_id}.jsonl"
        prog.parent.mkdir(parents=True, exist_ok=True)
        with prog.open("a", encoding="utf-8") as fh:
            for ev, data in self._events:
                rec = {"ts": 1763300000.0, "kind": "turn_event", "event": ev,
                       "data": {"frame_session": REMOTE_SID, "turn_id": REMOTE_TID, **data}}
                fh.write(json.dumps(rec) + "\n")
        return SimpleNamespace(
            state=TaskState.COMPLETED, text="节点终稿", artifacts=[],
            denials=[], session_id=REMOTE_SID, error="",
        )


async def test_integration_real_server_sse_frames_parsed(tmp_path, isolated_home):
    """真 server 的 _sse_frame 输出 × 真 client 解析：progress/status 全对齐。"""
    from ginno_runtime.a2a import server

    deps = _integration_deps(tmp_path)
    deps.executor = _SinkLikeExecutor([
        ("tool.start", {"name": "bash", "id": "tc1"}),
        ("tool.end", {"id": "tc1", "content": "ok"}),
        ("token.delta", {"content": "节点终稿"}),
    ])
    token = deps.tokens.issue()
    app = server.build_a2a_app(deps)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://a2a.test"
    ) as http:
        c = A2AClient(f"http://a2a.test/a2a/{token}", http_client=http)
        got = await _collect(c.stream_message("干活"))
    kinds = [f["event"] for f in got]
    assert kinds[0] == "status" and got[0]["data"]["status"]["state"] == "working"
    assert kinds[-1] == "status" and got[-1]["data"]["status"]["state"] == "completed"
    prog = [f["data"] for f in got if f["event"] == "progress"]
    assert [p["event"] for p in prog] == ["tool.start", "tool.end", "token.delta"]
    assert all(p["kind"] == "turn_event" for p in prog)
    assert prog[0]["data"]["frame_session"] == REMOTE_SID  # 改写是消费侧的事
    assert sum(1 for f in got if f["event"] == "status"
               and f["data"]["status"]["state"] == "completed") == 1


async def test_integration_run_remote_turn_over_real_server(wire, monkeypatch, tmp_path):
    """执行器 × 真 client × 真 server 全链路：工具事件到本地 socket 且归属正确。"""
    from ginno_runtime.a2a import peers as peers_mod, server

    deps = _integration_deps(tmp_path)
    deps.executor = _SinkLikeExecutor([
        ("tool.start", {"name": "bash", "id": "tc1"}),
        ("tool.end", {"id": "tc1", "content": "ok"}),
        ("token.delta", {"content": "节点终稿"}),
    ])
    token = deps.tokens.issue()
    app = server.build_a2a_app(deps)
    sid = "local-integ-1"
    sent: list[dict] = []

    class FakeWS:
        async def send_text(self, data):
            sent.append(json.loads(data))

    monkeypatch.setattr(rt, "_push_session_event", shared._push_session_event)
    monkeypatch.setattr(shared, "_SESSION_WS", {sid: [FakeWS()]})
    rec = peers_mod.PeerRecord(slug="vps", endpoint=f"http://a2a.test/a2a/{token}")
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://a2a.test"
    ) as http:
        monkeypatch.setattr(
            rt, "_client",
            lambda rec: A2AClient(rec.endpoint, http_client=http),
        )
        sess = {"id": sid, "remote_peer": "vps", "graph": None}
        res = await rt.run_remote_turn(sess, "干活", "tid-integ")
    assert res["ok"] is True, res.get("error")
    assert res["text"] == "节点终稿"
    assert sent, "本地 socket 必须收到帧"
    for f in sent:
        assert f.get("frame_session") == sid, f"归属未改写: {f}"
    assert REMOTE_SID not in json.dumps(sent)
    tool_starts = [f for f in sent if f["event"] == "tool.start"
                   and f.get("name") == "bash"]
    assert tool_starts, "远端工具调用必须实时可见"
    assert any(f["event"] == "tool.end" for f in sent)
    deltas = [f["content"] for f in sent if f["event"] == "token.delta"]
    assert deltas == ["节点终稿"], f"终稿重复灌全文: {deltas}"