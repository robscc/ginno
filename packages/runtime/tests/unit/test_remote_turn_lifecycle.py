"""出站远程 turn 的本地生命周期（2026-11-18 修复）。

实锤背景：远程会话（remote_peer）的 turn 在远端 peer 执行，本地生命周期
账本（_RUNNING_TURNS）与遗言 WAL 此前完全不打点——dev-swap/restart 时
遗言 prepare 遍历账本只能抓到残留的过期条目（真机抓到上一轮的「hi」
turn），真正在途的远端任务不可见，重启后零恢复。

修复后契约（对齐本地 turn 同口径）：
1. run_remote_turn 进入即登记 _RUNNING_TURNS/_RUNNING_TURNS_AT + 写
   WAL 遗言（user_request_head = **真实请求**，非本地 checkpoint 残留）；
2. 流断连/轮询兜底期间账本在、遗言 prepare 捕获到真实请求头；
3. turn 正常/异常/被取消收口一律清账本（finally 加固——原清理段在可抛
   调用之后，一抛就漏）；
4. dev-swap(signal) 路径：mark_shutdown 后收口不删遗言（SIGTERM 兜底）；
5. 恢复：远程会话重发最后用户请求原文（非 lastwords 模板）；
   auto 自动续跑对 remote 一律拦下（挂起待确认，防重复派活）。

执行器真跑（只在 _client seam 注入假 client，不桩 run_remote_turn——
假绿教训见 test_remote_turn_stream.py 头注）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import lastwords, server_shared as shared
from ginno_runtime.a2a.errors import A2AError
from ginno_runtime.api.stream import remote_turn as rt

pytestmark = pytest.mark.unit

SID = "97b8c71e42dd4cee9002a82d19dcb079"  # 实锤会话 id（纪念）
REAL_REQUEST = "拉 ginno 到 vps 排查 Linux 问题，corepack pnpm 缓存修复到一半"
REMOTE_SID = "remote-sess-999"  # 远端引擎盖的 frame_session（≠本地）
REMOTE_TID = "remote-turn-777"


def _status_frame(state, *, task_id="task-1", ctx="ctx-remote-1", text=None):
    d = {"id": task_id, "contextId": ctx, "status": {"state": state}}
    if text is not None:
        d["status"]["message"] = {"role": "agent", "parts": [{"kind": "text", "text": text}]}
    return {"event": "status", "data": d}


def _turn_event_frame(event, data, *, task_id="task-1"):
    stamped = {"frame_session": REMOTE_SID, "turn_id": REMOTE_TID, **data}
    return {"event": "progress", "data": {"taskId": task_id, "ts": 1763300000.0,
                                          "kind": "turn_event", "event": event,
                                          "data": stamped}}


def _fake_task(state, text=None, task_id="task-1", ctx="ctx-remote-1"):
    msg = None
    if text is not None:
        msg = SimpleNamespace(parts=[SimpleNamespace(text=text)])
    return SimpleNamespace(
        id=task_id, context_id=ctx,
        status=SimpleNamespace(state=state, message=msg), artifacts=[],
    )


class FakeGraph:
    def __init__(self):
        self.updates: list = []

    async def aupdate_state(self, config, update, as_node=None):
        self.updates.append((config, update))


class StalledRemoteClient:
    """中途断流 + 轮询永远 working——模拟「远端任务在途、本地被掐」。

    stream：吐 status(working) + 若干 tool/token 帧后抛 A2AError（conn
    reset）；此后 get_task 永远回 working（远端任务还在跑）。
    """

    def __init__(self, *, frames=None):
        self.frames = frames or [
            _status_frame("working"),
            _turn_event_frame("tool.start", {"name": "bash", "id": "tc1"}),
            _turn_event_frame("tool.end", {"id": "tc1", "content": "corepack 缓存路径…"}),
            _turn_event_frame("token.delta", {"content": "正在修 pnpm 缓存"}),
        ]
        self.polls = 0

    async def stream_message(self, text, *, context_id=None, reference_task_ids=None):
        for f in self.frames:
            yield f
        raise A2AError("connection reset by peer")

    async def get_task(self, task_id):
        self.polls += 1
        await asyncio.sleep(0)
        return _fake_task("working")

    async def cancel_task(self, task_id):
        return {}

    async def aclose(self):
        pass


class DoneRemoteClient(StalledRemoteClient):
    """流式正常到终态。"""

    def __init__(self):
        super().__init__()
        self.frames.append(_status_frame("completed", text="修好了"))

    async def get_task(self, task_id):
        return _fake_task("completed", text="修好了")


@pytest.fixture(autouse=True)
def _clean_lastwords_state():
    """清 lastwords 进程内状态（conftest 只清 _RUNNING_TURNS）。"""
    lastwords._REMOTE_HEADS.clear()
    lastwords._EFFORT.clear()
    lastwords._LAST_WRITE_AT.clear()
    lastwords._PENDING_RESUME.clear()
    lastwords._SHUTTING_DOWN = False
    yield
    lastwords._REMOTE_HEADS.clear()
    lastwords._EFFORT.clear()
    lastwords._LAST_WRITE_AT.clear()
    lastwords._PENDING_RESUME.clear()
    lastwords._SHUTTING_DOWN = False


@pytest.fixture
def wire(monkeypatch, isolated_home):
    """隔离 home + 假 peer 记录 + 可注入 client + session 注册。

    session 注册进 shared._SESSIONS（monkeypatch.setitem 自动还原）——
    write_lastwords 的 _resolve_slug 走内存表，无需磁盘 meta。
    """
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(
        slug="ginno-node-vps", endpoint="http://127.0.0.1:18790/a2a/CAP",
        task_timeout_s=30,
    )
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    monkeypatch.setattr(rt, "_POLL_S", 0.01)

    events: list = []

    async def fake_push(sid, ev, data, tid=None):
        events.append((sid, ev, data, tid))

    monkeypatch.setattr(rt, "_push_session_event", fake_push)

    async def fake_persist(session, user_text, reply):
        pass

    monkeypatch.setattr(rt, "_persist_exchange", fake_persist)

    sess = {
        "session_id": SID,
        "id": SID,
        "project_slug": "default",
        "remote_peer": "ginno-node-vps",
        "agent_id": "agent-1",
        "graph": FakeGraph(),
    }
    monkeypatch.setitem(shared._SESSIONS, SID, sess)
    yield SimpleNamespace(events=events, sess=sess, home=isolated_home, rec=rec)


def _install(monkeypatch, client):
    monkeypatch.setattr(rt, "_client", lambda rec: client)
    return client


def _read_snap(sid: str) -> dict:
    return json.loads(lastwords._path(sid).read_text())


async def _start_turn(wire, monkeypatch, client) -> asyncio.Task:
    _install(monkeypatch, client)
    return asyncio.create_task(
        rt.run_remote_turn(wire.sess, REAL_REQUEST, "tid-linux")
    )


async def _wait_polling(client: StalledRemoteClient, *, min_polls: int = 2) -> None:
    for _ in range(200):
        await asyncio.sleep(0.02)
        if client.polls >= min_polls:
            return
    raise AssertionError(f"轮询兜底未跑起（polls={client.polls}）")


# --------------------------------------------------------------------------- #
# 1) 断流在途：账本在 + 遗言捕获真实请求（非历史残留）
# --------------------------------------------------------------------------- #

async def test_broken_stream_keeps_ledger_and_captures_real_request(
    wire, monkeypatch
):
    """①流断连且远端仍在跑：账本/心跳在，dev-swap prepare 捕获真实请求。

    对照组：本地 checkpoint 残留上一轮「hi」问答（messages_for 注入），
    未修路径的 snapshot_from_messages 会产出 user_request_head='hi'。
    """
    stale = [
        SimpleNamespace(type="human", content="hi", additional_kwargs={}),
        SimpleNamespace(type="ai", content="你好！我是远端 Ginno。",
                        additional_kwargs={}, tool_calls=[]),
    ]
    monkeypatch.setattr(lastwords, "_load_thread_messages", lambda slug, sid: stale)

    client = StalledRemoteClient()
    task = await _start_turn(wire, monkeypatch, client)
    try:
        await _wait_polling(client)
        # 账本：真实 turn_id 在册 + 心跳戳在
        assert shared._RUNNING_TURNS.get(SID) == "tid-linux"
        assert SID in shared._RUNNING_TURNS_AT
        # WAL 遗言（turn 启动即写）：请求头是**真实请求**
        snap = _read_snap(SID)
        assert snap["turn_id"] == "tid-linux"
        assert snap["user_request_head"] == REAL_REQUEST
        assert snap["remote"] is True
        assert snap["peer"] == "ginno-node-vps"
        # 注意：初始 WAL 受 5s 节流不随 tool 边界重写（计数在内存）——
        # 真实计数随下方 write_lastwords 落盘。

        # dev-swap prepare 路径：write_lastwords 重写快照，仍指向真实 turn
        n = lastwords.write_lastwords(lastwords.REASON_DEV_SWAP)
        assert n == 1
        snap2 = _read_snap(SID)
        assert snap2["reason"] == "dev-swap"
        assert snap2["turn_id"] == "tid-linux"
        assert snap2["user_request_head"] == REAL_REQUEST, (
            "遗言捕获必须是真实在途请求，而非 checkpoint 残留的『hi』"
        )
        assert snap2["remote"] is True
        assert snap2["effort"]["tool_calls"] == 1, "tool.start 的 bump 应随快照落盘"
        # 流式尾部 = 真实进展（非上一轮问候语）
        assert "pnpm 缓存" in snap2["last_agent_message"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------------- #
# 2) 正常结束：账本清空 + 遗言删除
# --------------------------------------------------------------------------- #

async def test_completed_turn_clears_ledger_and_lastwords(wire, monkeypatch):
    """②流正常到终态：_RUNNING_TURNS/AT 清空、WAL 遗言删除、注册表注销。"""
    task = await _start_turn(wire, monkeypatch, DoneRemoteClient())
    res = await asyncio.wait_for(task, timeout=5)
    assert res["ok"] is True, res.get("error")
    assert SID not in shared._RUNNING_TURNS
    assert SID not in shared._RUNNING_TURNS_AT
    assert not lastwords._path(SID).exists(), "turn 正常结束遗言应删除"
    assert SID not in lastwords._REMOTE_HEADS


async def test_push_failure_still_clears_ledger(wire, monkeypatch):
    """③收口段事件推送抛异常（原实锤的残留路径）：finally 仍清账本。

    原实现的清理在 _push_session_event 之后——push 抛异常即跳过 pop，
    『hi』turn 的登记就是这样残留的。现在 finally 兜底。
    """
    client = DoneRemoteClient()
    _install(monkeypatch, client)
    boom = {"armed": False}

    async def push_boom(sid, ev, data, tid=None):
        # message.end（收口段）到达时引爆
        if ev == "message.end":
            boom["armed"] = True
            raise RuntimeError("ws push broken")
        wire.events.append((sid, ev, data, tid))

    monkeypatch.setattr(rt, "_push_session_event", push_boom)
    with contextlib.suppress(RuntimeError):
        await asyncio.wait_for(
            rt.run_remote_turn(wire.sess, REAL_REQUEST, "tid-boom"), timeout=5
        )
    assert boom["armed"], "引爆点未触发，测试无效"
    assert SID not in shared._RUNNING_TURNS, "push 抛异常后账本必须被 finally 清掉"


# --------------------------------------------------------------------------- #
# 3) signal 路径：mark_shutdown 后收口不删遗言（SIGTERM 兜底）
# --------------------------------------------------------------------------- #

async def test_signal_path_keeps_lastwords_during_shutdown(wire, monkeypatch):
    """④dev-swap 信号链路：turn 被取消（loop teardown 同款）时，
    write_lastwords(signal) 刚写的遗言不被 finally 删除。"""
    client = StalledRemoteClient()
    task = await _start_turn(wire, monkeypatch, client)
    try:
        await _wait_polling(client)
        lastwords.mark_shutdown()
        n = lastwords.write_lastwords(lastwords.REASON_SIGNAL)
        assert n == 1
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # 取消收口清了账本，但优雅关闭中不删遗言
        assert SID not in shared._RUNNING_TURNS
        snap = _read_snap(SID)
        assert snap["reason"] == "signal"
        assert snap["user_request_head"] == REAL_REQUEST
        assert snap["turn_id"] == "tid-linux"
    finally:
        lastwords._SHUTTING_DOWN = False
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# --------------------------------------------------------------------------- #
# 4) 恢复语义：远程会话重发最后用户请求原文
# --------------------------------------------------------------------------- #

async def test_resume_remote_reresends_last_user_request(wire, monkeypatch):
    """⑤_run_lastwords_turn 对 remote 会话：重发 user_request_head 原文，
    绝不用 lastwords 模板文本（远端没经历重启，模板指示反而误导）。"""
    captured: list = []

    async def fake_run_stream(ws, graph, config, text, session, agent_id, **kw):
        captured.append(text)

    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_run_stream)
    snap = {"user_request_head": REAL_REQUEST, "turn_id": "tid-linux",
            "reason": "dev-swap", "session_id": SID}
    ok = await lastwords._run_lastwords_turn(wire.sess, snap)
    assert ok is True
    assert captured == [REAL_REQUEST], "远程会话恢复必须重发最后用户请求原文"


async def test_recover_auto_blocked_for_remote(wire, monkeypatch):
    """⑥auto 模式对 remote 会话不自动直跑（防重复派活），挂起待确认。"""
    lastwords._write_atomic(
        lastwords._path(SID),
        {"session_id": SID, "project": "default", "reason": "dev-swap",
         "user_request_head": REAL_REQUEST, "last_agent_message": "",
         "running_tools": [], "bg_tasks": [], "bg_interrupted": [],
         "ts": time.time(), "turn_id": "tid-linux",
         "effort": {"tool_calls": 5, "started_at": 0, "turn_id": "tid-linux"},
         "remote": True},
    )
    monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session",
                        lambda sid: wire.sess)
    monkeypatch.setattr(lastwords, "mode", lambda: "auto")

    ran: list = []

    async def fake_turn(session, snap):
        ran.append(snap)
        return True

    monkeypatch.setattr(lastwords, "_run_lastwords_turn", fake_turn)

    pushed: list = []

    async def fake_push(event, data):
        pushed.append((event, data))

    monkeypatch.setattr(shared, "_push_global_event", fake_push)

    n = await lastwords.recover_lastwords()
    assert n == 0, "挂起待确认不计 restored"
    assert not ran, "auto 对 remote 会话绝不能自动直跑（重复派活风险）"
    assert SID in lastwords._PENDING_RESUME, "应挂起待用户确认"
    assert any(e == "runtime.lastwords_pending" for e, _ in pushed)
    assert any(d.get("remote") for _, d in pushed), "通知应带 remote 标记"
    # 文件保留（resume/dismiss 才处置）
    assert lastwords._path(SID).exists()
