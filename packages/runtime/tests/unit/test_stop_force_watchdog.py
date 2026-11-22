"""stop 强杀兜底（2026-11-22 僵死 turn 事故修复 PR②）单测。

事故：子代理 turn 僵死 4 小时对协作式 stop（_TURN_STOP 事件）无响应——
ws/control.handle_stop 的级联是 cooperative events only，死循环 turn 永不
收敛，_RUNNING_TURNS 永不摘除，用户终止无效。

修复：handle_stop 挂一次性延迟 watchdog（STOP_FORCE_DELAY_S，默认 60s）：
turn 仍未收敛（同一 turn_id 仍登记或同一 task 未完成）时——task.cancel()
（CancelledError 不进 except Exception → 不发 error 卡，走 job finally 的
注册表清理）+ ABANDONED_TURNS 防晚写 + 摘登记 + 发 turn.stopped 终态事件 +
子代理置 stopped 终态。身份快照（turn_id + task 对象）保证不误杀已收敛的
turn 与后继新 turn。

覆盖：
- 死循环 turn（await Event() 卡住、不响应协作停止）被 stop+watchdog 强制
  收敛：task cancelled、登记摘除、turn.stopped 事件、ABANDONED_TURNS 防晚写；
- managed turn（无 _TURN_TASKS 的子代理 turn）同样摘除 + 置 stopped 终态；
- 正常收敛的 turn：watchdog no-op（不发重复 turn.stopped、不 ABANDON）；
- 后继新 turn 不误杀（身份不匹配 → no-op）；
- idle stop 不挂 watchdog。
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from ginno_runtime import server_shared as shared
from ginno_runtime.api.stream.ws import control
from ginno_runtime.checkpointer import ABANDONED_TURNS

pytestmark = pytest.mark.unit


class _Conn:
    """handle_stop 需要的最小 WsConn 形状。"""

    def __init__(self, session_id: str, session: dict) -> None:
        self.session_id = session_id
        self.session = session


def _main_session(session_id: str = "s-main") -> dict:
    return {
        "project_slug": "default",
        "id": session_id,
        "type": "main",  # 跳过 subagent 级联分支
    }


@pytest.fixture
def fast_watchdog(monkeypatch):
    """延迟压到 30ms + 事件广播捕获。"""
    monkeypatch.setattr(control, "STOP_FORCE_DELAY_S", 0.03)
    sent: list[tuple[str, dict, object]] = []
    recorded: list[str] = []

    async def _capture(sid, event, payload, turn_id=None):
        sent.append((event, dict(payload), turn_id))

    monkeypatch.setattr(control, "_push_session_event", _capture)
    return sent, recorded


async def _drain_bg(seconds: float = 0.5) -> None:
    """让 spawn_bg 的 watchdog 有机会跑完（sleep 压到 30ms 后 0.5s 足够）。"""
    await asyncio.sleep(seconds)


def _pop_all(sid: str, turn_id: str = "") -> None:
    shared._RUNNING_TURNS.pop(sid, None)
    shared._RUNNING_TURNS_AT.pop(sid, None)
    shared._TURN_TASKS.pop(sid, None)
    shared._TURN_STOP.pop(sid, None)
    shared._MANAGED_TURNS.pop(sid, None)
    ABANDONED_TURNS.discard(turn_id)


# --------------------------------------------------------------------------- #
# 场景①：不响应协作停止的死 turn 被强制收敛
# --------------------------------------------------------------------------- #

async def test_dead_turn_force_converged(isolated_home, monkeypatch, caplog, fast_watchdog):
    """死循环 turn（await Event() 永不返回、不检查 _TURN_STOP）→ stop 后
    watchdog：task cancelled、登记摘除、turn.stopped 事件、ABANDONED_TURNS
    防晚写、warning 日志。"""
    sent, _ = fast_watchdog
    sid, tid = "s-dead", "turn-dead-1"
    session = _main_session(sid)
    # goal 分支：无 goal → get_goal 返回 None 即跳过（isolated_home 空 home）
    release = asyncio.Event()

    async def _dead_turn() -> None:
        try:
            await release.wait()  # 模拟僵死：永不响应协作事件
        except asyncio.CancelledError:
            raise
        finally:
            # 真实 _turn_job 的 finally 语义：identity 清理
            if shared._TURN_TASKS.get(sid) is asyncio.current_task():
                shared._TURN_TASKS.pop(sid, None)
            shared._TURN_STOP.pop(sid, None)

    task = asyncio.get_running_loop().create_task(_dead_turn())
    shared._TURN_TASKS[sid] = task
    shared._RUNNING_TURNS[sid] = tid
    shared._RUNNING_TURNS_AT[sid] = time.time()
    shared._TURN_STOP[sid] = asyncio.Event()
    try:
        with caplog.at_level(logging.WARNING, logger="ginno"):
            await control.handle_stop(_Conn(sid, session), {})
            await _drain_bg()
        # 协作式事件已 set（死 turn 不理它），watchdog 收敛后摘除毒事件：
        # 残留的 set 条目会让下一个 turn setdefault 到预置事件上秒死。
        evt = shared._TURN_STOP.get(sid)
        assert evt is None or not evt.is_set()
        # 强制收敛：
        assert task.cancelled() is True  # cancel 生效
        assert sid not in shared._RUNNING_TURNS  # 登记摘除
        assert tid in ABANDONED_TURNS  # 防晚写：cancel 后 checkpoint 写被拒
        stopped = [e for e in sent if e[0] == "turn.stopped"]
        assert stopped and stopped[0][2] == tid  # 终态事件（stopped 语义）
        assert any("turn_stop_watchdog_forced" in r.message for r in caplog.records)
    finally:
        release.set()
        _pop_all(sid, tid)
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def test_dead_turn_checkpoint_writes_blocked_after_force(isolated_home, fast_watchdog):
    """防晚写实际生效：强制收敛后 FileCheckpointer.aput 对该 turn_id 拒绝。"""
    from ginno_runtime.checkpointer import FileCheckpointer

    sent, _ = fast_watchdog
    sid, tid = "s-dead2", "turn-dead-2"
    release = asyncio.Event()

    async def _dead_turn() -> None:
        await release.wait()

    task = asyncio.get_running_loop().create_task(_dead_turn())
    shared._TURN_TASKS[sid] = task
    shared._RUNNING_TURNS[sid] = tid
    try:
        await control.handle_stop(_Conn(sid, _main_session(sid)), {})
        await _drain_bg()
        assert tid in ABANDONED_TURNS
        cp = FileCheckpointer(project_slug="default")
        cfg = {"configurable": {"thread_id": sid, "turn_id": tid}}
        out = await cp.aput(cfg, None, {}, {})  # None checkpoint: 只验证 guard
        assert out is cfg  # abandoned：拒绝持久化，原样返回 config
    finally:
        release.set()
        _pop_all(sid, tid)
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# --------------------------------------------------------------------------- #
# 场景②：managed turn（无 _TURN_TASKS）同样摘除 + stopped 终态
# --------------------------------------------------------------------------- #

async def test_managed_subagent_turn_force_finalized(
    isolated_home, monkeypatch, caplog, fast_watchdog
):
    """子代理 managed turn（_MANAGED_TURNS，无 _TURN_TASKS）→ 摘 _RUNNING_TURNS
    / _MANAGED_TURNS + turn.stopped + on_turn_stopped 置终态。"""
    sent, _ = fast_watchdog
    calls: list[str] = []

    async def _fake_on_turn_stopped(session_id):
        calls.append(session_id)

    monkeypatch.setattr(
        "ginno_runtime.subagent_scheduler.on_turn_stopped", _fake_on_turn_stopped
    )
    sid, tid = "s-managed", "turn-m1"
    session = {**_main_session(sid), "type": "subagent"}
    shared._RUNNING_TURNS[sid] = tid
    shared._RUNNING_TURNS_AT[sid] = time.time()
    shared._MANAGED_TURNS[sid] = tid
    shared._TURN_STOP[sid] = asyncio.Event()
    try:
        with caplog.at_level(logging.WARNING, logger="ginno"):
            await control.handle_stop(_Conn(sid, session), {})
            await _drain_bg()
        assert sid not in shared._RUNNING_TURNS
        assert sid not in shared._MANAGED_TURNS
        assert tid in ABANDONED_TURNS
        assert [e for e in sent if e[0] == "turn.stopped" and e[2] == tid]
        assert calls == [sid]  # 子代理 stopped 终态钩子被调用
        assert any("turn_stop_watchdog_forced" in r.message for r in caplog.records)
    finally:
        _pop_all(sid, tid)


# --------------------------------------------------------------------------- #
# 场景③④：不误伤——已收敛 / 后继新 turn
# --------------------------------------------------------------------------- #

async def test_converged_turn_watchdog_noop(isolated_home, fast_watchdog):
    """协作停止正常生效（turn 在窗口内自己结束）→ watchdog no-op：不发
    重复 turn.stopped、不 ABANDON。"""
    sent, _ = fast_watchdog
    sid, tid = "s-ok", "turn-ok-1"
    stop_evt = asyncio.Event()

    async def _good_turn() -> None:
        # 响应协作停止的 turn：等事件 → 正常收尾（finally 摘登记）
        try:
            await stop_evt.wait()
        finally:
            shared._RUNNING_TURNS.pop(sid, None)
            shared._RUNNING_TURNS_AT.pop(sid, None)
            shared._TURN_TASKS.pop(sid, None)
            shared._TURN_STOP.pop(sid, None)

    task = asyncio.get_running_loop().create_task(_good_turn())
    shared._TURN_TASKS[sid] = task
    shared._RUNNING_TURNS[sid] = tid
    shared._RUNNING_TURNS_AT[sid] = time.time()
    shared._TURN_STOP[sid] = stop_evt
    try:
        await control.handle_stop(_Conn(sid, _main_session(sid)), {})
        await asyncio.sleep(0.05)  # turn 收尾
        await _drain_bg()
        assert task.cancelled() is False  # 未被强杀
        assert tid not in ABANDONED_TURNS
        assert not [e for e in sent if e[0] == "turn.stopped"]  # 不补发
    finally:
        _pop_all(sid, tid)
        if not task.done():
            stop_evt.set()
            await asyncio.wait_for(task, 1)


async def test_new_turn_not_killed_by_old_stop_watchdog(isolated_home, fast_watchdog):
    """stop 后 60s 窗口内用户开了新 turn：身份不匹配（新 turn_id + 新 task）
    → watchdog no-op，新 turn 登记与 task 完好。"""
    sent, _ = fast_watchdog
    sid = "s-next"
    # 旧 turn：会被 stop + 正常收敛
    old_tid = "turn-old"
    old_stop = asyncio.Event()

    async def _old_turn() -> None:
        try:
            await old_stop.wait()
        finally:
            shared._RUNNING_TURNS.pop(sid, None)
            shared._RUNNING_TURNS_AT.pop(sid, None)
            shared._TURN_TASKS.pop(sid, None)
            shared._TURN_STOP.pop(sid, None)

    old_task = asyncio.get_running_loop().create_task(_old_turn())
    shared._TURN_TASKS[sid] = old_task
    shared._RUNNING_TURNS[sid] = old_tid
    shared._RUNNING_TURNS_AT[sid] = time.time()
    shared._TURN_STOP[sid] = old_stop
    # 新 turn：stop 之后立刻开始（新 turn_id、新 task、新事件）
    new_tid = "turn-new"
    new_stop = asyncio.Event()

    async def _new_turn() -> None:
        await new_stop.wait()

    try:
        await control.handle_stop(_Conn(sid, _main_session(sid)), {})
        # 旧 turn 收敛
        await asyncio.sleep(0.02)
        # 新 turn 接棒
        new_task = asyncio.get_running_loop().create_task(_new_turn())
        shared._TURN_TASKS[sid] = new_task
        shared._RUNNING_TURNS[sid] = new_tid
        shared._RUNNING_TURNS_AT[sid] = time.time()
        shared._TURN_STOP[sid] = new_stop
        await _drain_bg()
        assert shared._RUNNING_TURNS.get(sid) == new_tid  # 新 turn 未被摘
        assert not new_task.cancelled() and not new_task.done()
        assert new_tid not in ABANDONED_TURNS
        new_task.cancel()
        try:
            await new_task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        old_stop.set()
        _pop_all(sid, old_tid)
        ABANDONED_TURNS.discard(new_tid)


async def test_idle_stop_arms_no_watchdog(isolated_home, fast_watchdog, monkeypatch):
    """idle 会话上的 stop：不挂 watchdog（无副作用）。"""
    fired: list[str] = []

    async def _spy(*a, **k):
        fired.append("watchdog")

    monkeypatch.setattr(control, "_stop_force_watchdog", _spy)
    sid = "s-idle"
    await control.handle_stop(_Conn(sid, _main_session(sid)), {})
    await _drain_bg(0.1)
    assert fired == []
    assert not [e for e in fast_watchdog[0] if e[0] == "turn.stopped"]
