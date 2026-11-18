"""远程会话 runtime 安全网（审计单 C1，2026-11-18）。

覆盖四组缺口 + 五个小修：
- P0 goal fail-closed：PUT goal 对 remote_peer 会话拒绝（API 层）+
  driver 循环入口二次守卫（不派活）。
- P0 附件 fail-loud：remote 会话带 images/files/mention/skill 的 invoke
  整轮拒绝 + notice + message.end 解锁，绝不静默剥离。
- P1 stop 传播：用户 ⏹（_TURN_STOP 置位）在流式/轮询循环内生效——
  退出循环、cancel 对端 task、turn.stopped 收口、不再收帧。
- P1 权限卡：远端 permission.request 转发改为说明卡（notice），不再渲染
  可点 allow/deny（点了也只会被 ws 静默丢弃）。
- 小修：①title_gen 补跑 ②message.end 载荷 text+origin ③last_error 落
  meta ④/compact fail-loud ⑤导出头部执行节点行。

测试范式照抄 test_remote_turn_lifecycle.py：只在 _client seam 注入假
client，真跑 run_remote_turn / 真跑 _run_stream 拦截分支。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from ginno_runtime import server_shared as shared
from ginno_runtime.api import sessions as sessions_mod
from ginno_runtime.api.stream import remote_turn as rt
from ginno_runtime.api.stream import turn as turn_mod
from ginno_runtime.session_meta import _session_meta_upsert as _meta_upsert

pytestmark = pytest.mark.unit

SID = "sess-remote-safety"
SLUG = "default"


# --------------------------------------------------------------------------- #
# 共享 fake peer 基建（照抄 test_remote_turn_lifecycle 的构造法）
# --------------------------------------------------------------------------- #
def _status_frame(state, *, task_id="task-1", ctx="ctx-remote-1", text=None):
    d = {"id": task_id, "contextId": ctx, "status": {"state": state}}
    if text is not None:
        d["status"]["message"] = {"role": "agent",
                                  "parts": [{"kind": "text", "text": text}]}
    return {"event": "status", "data": d}


def _fake_task(state, text=None, task_id="task-1", ctx="ctx-remote-1"):
    msg = None
    if text is not None:
        msg = SimpleNamespace(parts=[SimpleNamespace(text=text)])
    return SimpleNamespace(
        id=task_id, context_id=ctx,
        status=SimpleNamespace(state=state, message=msg), artifacts=[],
    )


class ChattyRemoteClient:
    """持续吐帧的假对端：status(working) 后每 20ms 一条 token.delta，永不到
    终态（直到被 stop/cancel 打断）——stop 检查必须在这种「活流」里生效。"""

    def __init__(self, *, interval=0.02):
        self.interval = interval
        self.consumed = 0
        self.cancelled: list[str] = []
        self.polls = 0

    async def stream_message(self, text, *, context_id=None, reference_task_ids=None):
        yield _status_frame("working")
        self.consumed += 1
        i = 0
        while True:
            await asyncio.sleep(self.interval)
            yield {"event": "progress", "data": {
                "taskId": "task-1", "ts": time.time(), "kind": "turn_event",
                "event": "token.delta",
                "data": {"frame_session": "remote-x", "turn_id": "remote-t",
                         "content": f"chunk-{i} "},
            }}
            self.consumed += 1
            i += 1

    async def get_task(self, task_id):
        self.polls += 1
        return _fake_task("working")

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return {}

    async def aclose(self):
        pass


class DoneRemoteClient:
    """一次性流到终态。"""

    def __init__(self):
        self.cancelled: list[str] = []

    async def stream_message(self, text, *, context_id=None, reference_task_ids=None):
        yield _status_frame("working")
        yield {"event": "progress", "data": {
            "taskId": "task-1", "ts": time.time(), "kind": "turn_event",
            "event": "permission.request",
            "data": {"frame_session": "remote-x", "turn_id": "remote-t",
                     "tool": "bash", "args": {"command": "ls"}},
        }}
        yield _status_frame("completed", text="干完了")

    async def get_task(self, task_id):
        return _fake_task("completed", text="干完了")

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return {}

    async def aclose(self):
        pass


class DeadPeerClient:
    """建流即败（对端不可达）——失败路径：err + last_error 落 meta。"""

    from ginno_runtime.a2a.errors import A2AError as _A2AError

    def __init__(self):
        self.cancelled: list[str] = []

    async def stream_message(self, text, *, context_id=None, reference_task_ids=None):
        raise self._A2AError("peer unreachable")
        yield  # pragma: no cover — 生成器语法需要

    async def send_message(self, text, *, context_id=None, reference_task_ids=None):
        raise self._A2AError("peer unreachable")

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return {}

    async def aclose(self):
        pass


@pytest.fixture
def wire(monkeypatch, isolated_home):
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(
        slug="vps-node", endpoint="http://127.0.0.1:18790/a2a/CAP",
        task_timeout_s=30,
    )
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    monkeypatch.setattr(rt, "_POLL_S", 0.01)

    events: list = []

    async def fake_push(sid, ev, data, tid=None):
        events.append((sid, ev, data, tid))

    monkeypatch.setattr(rt, "_push_session_event", fake_push)

    async def fake_persist(session, user_text, reply, refs=None, **_kw):
        # refs：#213 重载还原锚点（本桩不关心），**_kw 前向兼容后续载荷扩展。
        events.append((session.get("session_id") or session.get("id"),
                       "__persist", reply, None))

    monkeypatch.setattr(rt, "_persist_exchange", fake_persist)

    sess = {
        "session_id": SID, "id": SID, "project_slug": SLUG,
        "remote_peer": "vps-node", "agent_id": "agent-1", "graph": None,
    }
    monkeypatch.setitem(shared._SESSIONS, SID, sess)
    yield SimpleNamespace(events=events, sess=sess)


def _evs(events, name):
    return [d for _, ev, d, _ in events if ev == name]


def _names(events):
    return [ev for _, ev, _, _ in events]


# --------------------------------------------------------------------------- #
# P1 stop 传播
# --------------------------------------------------------------------------- #
async def test_stop_cancels_remote_task_and_closes_with_turn_stopped(wire, monkeypatch):
    """⏹ 后：流循环退出、对端收到 cancel、turn.stopped 收口、不再收帧。"""
    client = ChattyRemoteClient()
    monkeypatch.setattr(rt, "_client", lambda rec: client)
    shared._TURN_STOP.pop(SID, None)

    task = asyncio.create_task(
        rt.run_remote_turn(wire.sess, "远端干活", "tid-stop")
    )
    try:
        # 等活流跑起来（若干 token.delta 已到）
        for _ in range(200):
            await asyncio.sleep(0.02)
            if len(_evs(wire.events, "token.delta")) >= 3:
                break
        assert len(_evs(wire.events, "token.delta")) >= 3, "活流未跑起，测试无效"
        _evt = shared._TURN_STOP.setdefault(SID, asyncio.Event())
        _evt.set()
        res = await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        shared._TURN_STOP.pop(SID, None)

    assert res["stopped"] is True and res["state"] == "canceled"
    # 对端收到 cancel（带真实 task id）
    assert client.cancelled == ["task-1"], "stop 必须 cancel 对端 task"
    # 收口帧：turn.stopped 在、且不发 message.end/done（本地 TurnStopped 同形）
    names = _names(wire.events)
    assert "turn.stopped" in names
    assert "message.end" not in names and "done" not in names
    # 部分回复已落本地镜像（含已流式片段）
    persisted = [d for _, ev, d, _ in wire.events if ev == "__persist"]
    assert persisted and "chunk-0" in persisted[-1]
    # 账本收口（#212 的 finally 保证）
    assert SID not in shared._RUNNING_TURNS


async def test_stop_in_poll_loop_also_cancels(wire, monkeypatch):
    """轮询兜底路径（断流后）里 stop 同样生效。"""

    class BrokenThenPollingClient(ChattyRemoteClient):
        async def stream_message(self, text, *, context_id=None, reference_task_ids=None):
            yield _status_frame("working")
            self.consumed += 1
            raise type(self)._boom  # 中途断流 → 轮询兜底

    from ginno_runtime.a2a.errors import A2AError

    BrokenThenPollingClient._boom = A2AError("connection reset")
    client = BrokenThenPollingClient()
    monkeypatch.setattr(rt, "_client", lambda rec: client)
    shared._TURN_STOP.pop(SID, None)

    task = asyncio.create_task(
        rt.run_remote_turn(wire.sess, "断流后轮询", "tid-stop2")
    )
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if client.polls >= 2:
                break
        assert client.polls >= 2, "轮询兜底未跑起，测试无效"
        shared._TURN_STOP.setdefault(SID, asyncio.Event()).set()
        res = await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        shared._TURN_STOP.pop(SID, None)

    assert res["stopped"] is True
    assert client.cancelled == ["task-1"]
    assert "turn.stopped" in _names(wire.events)


# --------------------------------------------------------------------------- #
# P1 权限卡 → 说明卡
# --------------------------------------------------------------------------- #
async def test_remote_permission_request_becomes_notice_not_card(wire, monkeypatch):
    """远端 permission.request 不转发原帧（会渲染成点了没反应的真卡），
    改发说明卡 notice。"""
    monkeypatch.setattr(rt, "_client", lambda rec: DoneRemoteClient())
    res = await asyncio.wait_for(
        rt.run_remote_turn(wire.sess, "要权限的活", "tid-perm"), timeout=5
    )
    assert res["ok"] is True, res.get("error")
    names = _names(wire.events)
    assert "permission.request" not in names, "不得再转发可点权限卡帧"
    hints = [d for d in _evs(wire.events, "notice")
             if d.get("i18n_key") == "stream.remote_permission_hint"]
    assert len(hints) == 1
    assert hints[0]["params"]["tool"] == "bash"
    assert "bash" in hints[0]["message"]
    # 流程照常收口（不被权限帧打断）
    assert "done" in names


# --------------------------------------------------------------------------- #
# 小修 ②：message.end 载荷 text + origin
# --------------------------------------------------------------------------- #
async def test_message_end_carries_text_and_origin(wire, monkeypatch):
    monkeypatch.setattr(rt, "_client", lambda rec: DoneRemoteClient())
    res = await asyncio.wait_for(
        rt.run_remote_turn(wire.sess, "hi", "tid-me", origin="deliver"), timeout=5
    )
    assert res["ok"] is True
    ends = _evs(wire.events, "message.end")
    assert len(ends) == 1
    assert ends[0]["text"].startswith("干完了")
    assert ends[0]["origin"] == "deliver"


# --------------------------------------------------------------------------- #
# 小修 ③：失败落 meta.last_error
# --------------------------------------------------------------------------- #
async def test_remote_failure_persists_last_error(wire, monkeypatch):
    _meta_upsert(SLUG, {"id": SID, "title": "远端会话", "type": "main",
                        "created": time.time(), "updated": time.time()})
    monkeypatch.setattr(rt, "_client", lambda rec: DeadPeerClient())
    res = await asyncio.wait_for(
        rt.run_remote_turn(wire.sess, "hi", "tid-fail"), timeout=5
    )
    assert res["ok"] is False
    from ginno_runtime.session_meta import _find_meta

    found = _find_meta(SID)
    assert found, "meta 应存在"
    le = (found[0] or {}).get("last_error") or {}
    assert le.get("turn_id") == "tid-fail"
    assert "peer unreachable" in str(le.get("message") or "")
    assert found[0].get("last_finished"), "未读收件箱对齐字段应随失败落盘"


# --------------------------------------------------------------------------- #
# 小修 ①：远端完成补跑 LLM 标题
# --------------------------------------------------------------------------- #
async def test_remote_completion_spawns_title_gen(wire, monkeypatch):
    _meta_upsert(SLUG, {"id": SID, "title": "截断占位…", "type": "main",
                        "created": time.time(), "updated": time.time(),
                        "title_llm_pending": True, "title_seed": "远端干活"})
    spawned: list = []
    from ginno_runtime import title_gen

    monkeypatch.setattr(title_gen, "spawn_title_gen",
                        lambda sid, tid: spawned.append((sid, tid)))
    monkeypatch.setattr(rt, "_client", lambda rec: DoneRemoteClient())
    res = await asyncio.wait_for(
        rt.run_remote_turn(wire.sess, "hi", "tid-title"), timeout=5
    )
    assert res["ok"] is True
    assert spawned == [(SID, "tid-title")], "完成必须补跑 title_gen（小修①）"
    from ginno_runtime.session_meta import _find_meta

    meta = _find_meta(SID)[0]
    assert meta.get("title_assistant") == "干完了"


async def test_remote_completion_skips_title_when_not_pending(wire, monkeypatch):
    """无 title_llm_pending（手动改名过/非首轮）不 spawn——与本地同语义。"""
    _meta_upsert(SLUG, {"id": SID, "title": "手改名", "type": "main",
                        "created": time.time(), "updated": time.time(),
                        "title_llm_pending": False})
    spawned: list = []
    from ginno_runtime import title_gen

    monkeypatch.setattr(title_gen, "spawn_title_gen",
                        lambda sid, tid: spawned.append((sid, tid)))
    monkeypatch.setattr(rt, "_client", lambda rec: DoneRemoteClient())
    await asyncio.wait_for(
        rt.run_remote_turn(wire.sess, "hi", "tid-title2"), timeout=5
    )
    assert spawned == []


# --------------------------------------------------------------------------- #
# P0 goal fail-closed：API 层
# --------------------------------------------------------------------------- #
async def test_put_goal_rejected_for_remote_session(isolated_home, monkeypatch):
    monkeypatch.setitem(
        sessions_mod._SESSIONS, SID,
        {"session_id": SID, "project_slug": SLUG, "agent_id": "dev",
         "remote_peer": "vps-node"},
    )
    r = await sessions_mod.set_session_goal(SID, {"objective": "远端目标"})
    assert r["ok"] is False
    assert r.get("remote") is True
    # 双语文案经 t()（en 断言语义关键词，zh 用户在 zh locale 收中文）
    assert "peer node" in r["error"], "拒绝原因必须说明目标由对端管理"
    from ginno_runtime.goals import store as goal_store

    assert goal_store.get_goal(SLUG, SID) is None, "goal 不得落盘"


async def test_put_goal_rejected_via_disk_meta(isolated_home):
    """会话未建图（仅磁盘 meta 带 remote_peer）同样拒绝。"""
    _meta_upsert(SLUG, {"id": SID, "title": "r", "type": "main",
                        "created": time.time(), "updated": time.time(),
                        "remote_peer": "vps-node"})
    r = await sessions_mod.set_session_goal(SID, {"objective": "x"})
    assert r["ok"] is False and r.get("remote") is True


async def test_put_goal_local_session_unaffected(isolated_home, monkeypatch):
    """对照：本地会话照常可设 goal（不被守卫误伤）。"""
    monkeypatch.setitem(
        sessions_mod._SESSIONS, SID,
        {"session_id": SID, "project_slug": SLUG, "agent_id": "dev"},
    )
    async def _noop_event(*a, **k):
        pass

    monkeypatch.setattr(sessions_mod, "_emit_goal_event", _noop_event)
    monkeypatch.setattr(sessions_mod, "_start_goal_driver", lambda sid: None)
    r = await sessions_mod.set_session_goal(SID, {"objective": "本地目标"})
    assert r["ok"] is True, r
    from ginno_runtime.goals import store as goal_store

    assert goal_store.get_goal(SLUG, SID)["status"] == "active"


# --------------------------------------------------------------------------- #
# P0 goal fail-closed：driver 循环入口二次守卫
# --------------------------------------------------------------------------- #
async def test_goal_driver_refuses_remote_session(isolated_home, monkeypatch):
    """remote 会话 + active goal（绕过 API 写入的）→ driver 立即退出，
    绝不派活。"""
    from ginno_runtime.goals import store as goal_store

    goal_store.replace_goal(SLUG, SID, "绕过 API 的目标", agent_id="dev")
    monkeypatch.setitem(
        sessions_mod._SESSIONS, SID,
        {"session_id": SID, "project_slug": SLUG, "agent_id": "dev",
         "graph": None, "remote_peer": "vps-node"},
    )
    dispatched: list = []

    async def _spy_run_goal_turn(session, goal, turn_id):
        dispatched.append(turn_id)

    monkeypatch.setattr(sessions_mod, "_run_goal_turn", _spy_run_goal_turn)

    sessions_mod._start_goal_driver(SID)
    await asyncio.sleep(0.2)  # 给 driver 一拍跑到入口守卫

    assert dispatched == [], "remote 会话绝不能由本地 driver 派活"
    assert sessions_mod._GOAL_DRIVERS.get(SID) is None, "driver 应已自行退出"
    # goal 保持原状（守卫不改写状态，只拒绝续跑）
    assert goal_store.get_goal(SLUG, SID)["status"] == "active"


# --------------------------------------------------------------------------- #
# P0 附件 fail-loud（turn.py 拦截处）
# --------------------------------------------------------------------------- #
async def test_remote_turn_with_attachments_rejected_loudly(monkeypatch):
    """images 任一非空 → notice + message.end，run_remote_turn 不被调。"""
    events: list = []

    async def fake_push(sid, ev, data, tid=None):
        events.append((sid, ev, data, tid))

    monkeypatch.setattr(turn_mod, "_push_session_event", fake_push)
    calls: list = []

    async def fake_remote(session, user_text, turn_id, *, agent_id=None, **kw):
        calls.append(user_text)

    monkeypatch.setattr(rt, "run_remote_turn", fake_remote)

    sess = {"session_id": SID, "project_slug": SLUG, "remote_peer": "vps-node",
            "graph": None}
    cfg = {"configurable": {"thread_id": SID, "turn_id": "tid-att"}}
    await turn_mod._run_stream(
        None, None, cfg, "看这张图", sess, None,
        images=[{"data": "aGk=", "media_type": "image/png"}],
    )
    assert calls == [], "带附件的远端轮必须被拒绝，不得转发"
    notices = [d for _, ev, d, _ in events if ev == "notice"]
    assert any(d.get("i18n_key") == "stream.remote_attachments_rejected"
               and d.get("kind") == "error" for d in notices), notices
    ends = [d for _, ev, d, _ in events if ev == "message.end"]
    assert ends, "拒绝后必须补 message.end 解锁 composer"


async def test_remote_turn_with_skill_rejected_loudly(monkeypatch):
    events: list = []

    async def fake_push(sid, ev, data, tid=None):
        events.append((sid, ev, data, tid))

    monkeypatch.setattr(turn_mod, "_push_session_event", fake_push)
    calls: list = []

    async def fake_remote(session, user_text, turn_id, *, agent_id=None, **kw):
        calls.append(user_text)

    monkeypatch.setattr(rt, "run_remote_turn", fake_remote)
    sess = {"session_id": SID, "project_slug": SLUG, "remote_peer": "vps-node",
            "graph": None}
    await turn_mod._run_stream(
        None, None, {"configurable": {"turn_id": "tid-sk"}}, "/browser 打开",
        sess, None, skill_name="browser",
    )
    assert calls == []
    assert any(d.get("i18n_key") == "stream.remote_attachments_rejected"
               for _, ev, d, _ in events if ev == "notice")


async def test_remote_turn_plain_text_still_forwards(monkeypatch):
    """对照：纯文本远端轮照常转发（守卫不误伤主路径）。"""
    calls: list = []

    async def fake_push(sid, ev, data, tid=None):
        pass

    monkeypatch.setattr(turn_mod, "_push_session_event", fake_push)

    async def fake_remote(session, user_text, turn_id, *, agent_id=None, **kw):
        calls.append((user_text, kw.get("origin")))

    monkeypatch.setattr(rt, "run_remote_turn", fake_remote)
    sess = {"session_id": SID, "project_slug": SLUG, "remote_peer": "vps-node",
            "graph": None}
    await turn_mod._run_stream(
        None, None, {"configurable": {"turn_id": "tid-ok"}}, "纯文本没问题",
        sess, None,
    )
    assert calls == [("纯文本没问题", "interactive")], "纯文本应透传且 origin 随行"


# --------------------------------------------------------------------------- #
# 小修 ④：/compact fail-loud
# --------------------------------------------------------------------------- #
async def test_compact_rejected_for_remote_session(isolated_home):
    from ginno_runtime.commands.registry import BUILTINS

    sess = {"session_id": SID, "project_slug": SLUG, "remote_peer": "vps-node",
            "graph": None}
    out = await BUILTINS["compact"].async_handler(SLUG, sess, "")
    assert "peer node" in out, out


# --------------------------------------------------------------------------- #
# 小修 ⑤：导出头部执行节点行
# --------------------------------------------------------------------------- #
def test_export_header_marks_remote_execution(isolated_home):
    from ginno_runtime.api.export import _render_markdown

    meta = {"id": SID, "title": "远端会话", "created": time.time(),
            "provider": "openai", "model": "gpt-x", "remote_peer": "vps-node"}
    md = _render_markdown(SLUG, SID, meta)
    assert "- 执行：远端节点" in md and "vps-node" in md
    assert "（对端模型）" in md


def test_export_header_local_unchanged(isolated_home):
    from ginno_runtime.api.export import _render_markdown

    meta = {"id": SID, "title": "本地会话", "created": time.time(),
            "provider": "openai", "model": "gpt-x"}
    md = _render_markdown(SLUG, SID, meta)
    assert "- 执行：远端节点" not in md
