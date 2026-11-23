"""读路径活性验证（2026-11-22 僵死 turn 事故修复 PR①）单测。

事故：子代理 turn 僵死 4 小时，_RUNNING_TURNS 永不摘除——badge running 与
/api/running 直读登记表无活性验证，侧栏「运行中」永久显示。修复：抽公共活性
读 running_turn_alive / live_running_turns（复用 turn_gate_busy 的三信号判定
核心），badge 与 /api/running 改经它读——stale 条目（>20min 无 live task）
就地 heal（摘除 + warning 日志）。

覆盖：
- running_turn_alive 三信号（live task / pending resume / AT 新鲜/陈旧/缺失）；
- live_running_turns 混合场景只留活条目，stale 就地 heal + 日志可断言；
- _heal=False 纯探针（不动注册表）；
- turn_gate_busy 复用核心后的回归（2026-10-31 泄漏自愈语义不变）。
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from ginno_runtime import server_shared as shared

pytestmark = pytest.mark.unit


def _arm(sid: str, tid: str, *, age_s: float | None) -> None:
    """Arm a _RUNNING_TURNS entry with an optional heartbeat age."""
    shared._RUNNING_TURNS[sid] = tid
    if age_s is not None:
        shared._RUNNING_TURNS_AT[sid] = time.time() - age_s


class _LiveTask:
    """duck-typed task stub: not done → definitive liveness."""

    def done(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# running_turn_alive：三信号
# --------------------------------------------------------------------------- #

def test_alive_with_live_task_even_if_stale():
    """信号①：_TURN_TASKS 活 → 永远计活（长工具 turn 的正统判定）。"""
    shared._TURN_TASKS["s-task"] = _LiveTask()
    _arm("s-task", "t-1", age_s=shared.RUNNING_TURN_STALE_S * 10)
    try:
        assert shared.running_turn_alive("s-task") is True
        assert "s-task" in shared._RUNNING_TURNS  # 不误 heal
    finally:
        shared._TURN_TASKS.pop("s-task", None)
        shared._RUNNING_TURNS.pop("s-task", None)
        shared._RUNNING_TURNS_AT.pop("s-task", None)


async def test_wake_task_only_keeps_its_own_turn_alive(monkeypatch):
    sid, tid = "s-wake", "t-wake"
    release = asyncio.Event()
    wake_task = asyncio.create_task(release.wait())
    monkeypatch.setitem(shared._WAKE_TURN_TASKS, (sid, tid), wake_task)
    try:
        _arm(sid, tid, age_s=shared.RUNNING_TURN_STALE_S + 5)
        assert shared.running_turn_alive(sid) is True

        # Once stop removes the current registration, the task cannot keep
        # this session busy, even if its coroutine is still unwinding.
        shared._RUNNING_TURNS.pop(sid)
        assert shared.running_turn_alive(sid) is False

        # An old task must not protect a leaked registration for a successor.
        _arm(sid, "t-leaked", age_s=shared.RUNNING_TURN_STALE_S + 5)
        assert shared.running_turn_alive(sid) is False
        assert sid not in shared._RUNNING_TURNS
        assert sid not in shared._RUNNING_TURNS_AT
    finally:
        release.set()
        await wake_task

    # A finished task is no evidence of liveness even for the same turn ID.
    _arm(sid, tid, age_s=shared.RUNNING_TURN_STALE_S + 5)
    assert shared.running_turn_alive(sid) is False
    assert sid not in shared._RUNNING_TURNS
    assert sid not in shared._RUNNING_TURNS_AT


def test_alive_when_parked_at_interrupt():
    """信号②：_PENDING_RESUME（ask_user/权限卡）→ 计活（parked 是活的）。"""
    _arm("s-park", "t-2", age_s=shared.RUNNING_TURN_STALE_S * 3)
    shared._PENDING_RESUME.add("s-park")
    try:
        assert shared.running_turn_alive("s-park") is True
    finally:
        shared._PENDING_RESUME.discard("s-park")
        shared._RUNNING_TURNS.pop("s-park", None)
        shared._RUNNING_TURNS_AT.pop("s-park", None)


def test_alive_fresh_arm_without_timestamp():
    """信号③a：刚 arm 无 AT 时间戳 → 保守计活（不能误杀起步中的 turn）。"""
    _arm("s-fresh", "t-3", age_s=None)
    try:
        assert shared.running_turn_alive("s-fresh") is True
    finally:
        shared._RUNNING_TURNS.pop("s-fresh", None)


def test_alive_within_stale_window():
    """信号③b：有 AT 且在窗口内 → 计活。"""
    _arm("s-mid", "t-4", age_s=60.0)
    try:
        assert shared.running_turn_alive("s-mid") is True
    finally:
        shared._RUNNING_TURNS.pop("s-mid", None)
        shared._RUNNING_TURNS_AT.pop("s-mid", None)


def test_stale_entry_healed_with_warning(caplog):
    """信号③c：超 STALE 窗口、无活 task、非 parked → 计死 + 就地 heal +
    warning 日志（与既有 turn_gate heal 同格式前缀，读路径 via 附注）。"""
    sid = "s-stale"
    _arm(sid, "t-zombie", age_s=shared.RUNNING_TURN_STALE_S + 5)
    with caplog.at_level(logging.WARNING, logger="ginno.turn"):
        assert shared.running_turn_alive(sid, via="badge") is False
    assert sid not in shared._RUNNING_TURNS
    assert sid not in shared._RUNNING_TURNS_AT
    heals = [r for r in caplog.records if "running_turn_stale_healed" in r.message]
    assert heals, "heal 必须留 warning 日志（可追）"
    assert f"session={sid}" in heals[0].getMessage()
    assert "turn=t-zombie" in heals[0].getMessage()


def test_probe_without_heal_leaves_registration():
    """_heal=False：纯探针——计死但不动注册表（查询方不担副作用）。"""
    sid = "s-probe"
    _arm(sid, "t-5", age_s=shared.RUNNING_TURN_STALE_S + 1)
    try:
        assert shared.running_turn_alive(sid, _heal=False) is False
        assert sid in shared._RUNNING_TURNS  # 未 heal
    finally:
        shared._RUNNING_TURNS.pop(sid, None)
        shared._RUNNING_TURNS_AT.pop(sid, None)


def test_alive_false_for_idle_session():
    """无任何登记 → 计死（O(1) 快速路径）。"""
    assert shared.running_turn_alive("s-idle") is False


# --------------------------------------------------------------------------- #
# live_running_turns：读路径快照 + heal
# --------------------------------------------------------------------------- #

def test_live_running_turns_filters_and_heals_mixed(caplog):
    """混合登记表：只返回活条目；stale 在遍历中就地 heal（快照迭代不炸）。"""
    _arm("z-stale", "t-z", age_s=shared.RUNNING_TURN_STALE_S * 4)  # 僵死
    _arm("a-fresh", "t-f", age_s=30.0)  # 新鲜
    _arm("a-notime", "t-n", age_s=None)  # 刚 arm 无时间戳
    _arm("a-parked", "t-p", age_s=shared.RUNNING_TURN_STALE_S * 4)  # parked
    shared._PENDING_RESUME.add("a-parked")
    shared._TURN_TASKS["a-tasked"] = _LiveTask()
    _arm("a-tasked", "t-k", age_s=shared.RUNNING_TURN_STALE_S * 4)  # 有活 task
    try:
        with caplog.at_level(logging.WARNING, logger="ginno.turn"):
            live = shared.live_running_turns(via="report")
        assert live == {
            "a-fresh": "t-f",
            "a-notime": "t-n",
            "a-parked": "t-p",
            "a-tasked": "t-k",
        }
        # 僵死条目被就地 heal，其余保留
        assert "z-stale" not in shared._RUNNING_TURNS
        assert set(shared._RUNNING_TURNS) == {"a-fresh", "a-notime", "a-parked", "a-tasked"}
        assert any(
            "running_turn_stale_healed" in r.message
            and "session=z-stale" in r.getMessage()
            for r in caplog.records
        )
    finally:
        for sid in ("z-stale", "a-fresh", "a-notime", "a-parked", "a-tasked"):
            shared._RUNNING_TURNS.pop(sid, None)
            shared._RUNNING_TURNS_AT.pop(sid, None)
        shared._TURN_TASKS.pop("a-tasked", None)
        shared._PENDING_RESUME.discard("a-parked")


def test_live_running_turns_empty_when_idle():
    assert shared.live_running_turns() == {}


# --------------------------------------------------------------------------- #
# turn_gate_busy：复用核心后的回归（2026-10-31 语义不变）
# --------------------------------------------------------------------------- #

def test_turn_gate_busy_still_heals_stale():
    """gate 路径经 running_turn_alive 复用后：stale → False + heal。"""
    _arm("g-stale", "t-g1", age_s=shared.RUNNING_TURN_STALE_S + 5)
    try:
        assert shared.turn_gate_busy("g-stale") is False
        assert "g-stale" not in shared._RUNNING_TURNS
    finally:
        shared._RUNNING_TURNS.pop("g-stale", None)
        shared._RUNNING_TURNS_AT.pop("g-stale", None)


def test_turn_gate_busy_fresh_arm_still_busy():
    _arm("g-fresh", "t-g2", age_s=None)
    try:
        assert shared.turn_gate_busy("g-fresh") is True
    finally:
        shared._RUNNING_TURNS.pop("g-fresh", None)


def test_turn_gate_busy_asyncio_task_liveness():
    """真实 asyncio.Task（非 duck-type）：活 → busy；done + 心跳陈旧 →
    泄漏 heal（task 结束后残留的登记 20min 后由读路径收敛）。"""

    async def _noop() -> None:
        pass

    async def _main() -> None:
        t = asyncio.ensure_future(_noop())
        _arm("g-task", "t-g3", age_s=30.0)  # 心跳新鲜
        shared._TURN_TASKS["g-task"] = t
        try:
            assert shared.turn_gate_busy("g-task") is True  # 活 task 定活
        finally:
            await t
        # task 已 done、心跳远超窗口、非 parked → 泄漏条目 heal + 不 busy
        shared._RUNNING_TURNS_AT["g-task"] = (
            time.time() - shared.RUNNING_TURN_STALE_S - 1
        )
        try:
            assert shared.turn_gate_busy("g-task") is False
            assert "g-task" not in shared._RUNNING_TURNS
        finally:
            shared._TURN_TASKS.pop("g-task", None)
            shared._RUNNING_TURNS.pop("g-task", None)
            shared._RUNNING_TURNS_AT.pop("g-task", None)

    asyncio.run(_main())
