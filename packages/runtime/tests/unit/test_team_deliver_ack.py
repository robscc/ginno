"""AgentTeam 投递确认语义（方案②事件驱动 ack，2026-11-18 待决策项 b）。

缺陷原状：deliver 用 `_await_turn_start` 盲轮询 `_RUNNING_TURNS`（50ms 间隔
/5s 窗口），而成员 turn 的登记发生在 runner 一长串前置（`_ensure_session`
的 graph 构建、checkpoint IO、事件循环拥堵）之后——前置超过 5s 窗口即
误报 stalled（干净 main 可复现，2026-11-17 实录）。

方案②：投递时注册 ack future，runner 登记 `_RUNNING_TURNS` 的瞬间推式
set（`server_shared.notify_turn_started`）；deliver 等 ack 事件而非盲轮询。
fail-safe：ack 超时（可配 `_ACK_TIMEOUT_S`）后回查 `_is_live`（老语义兜底）
——真死信仍报 stalled，ack 机制故障不冤枉已起来的 turn。

测试纪律：不桩 `_run_managed_turn`（那会把 ack 事件源一起桩掉）——只桩
IO 边界（`_ensure_session` / `_run_stream`），让真实 runner 走到登记点，
ack 链路端到端可测。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ginno_runtime import server_shared
from ginno_runtime.teams import store as team_store
from ginno_runtime.teams import deliver as deliver_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def member_iid(seeded_home):
    """建队并返回成员 instance_id（deliver 的定位键，非 agent_id）。"""
    t = team_store.create_team("队", "default", "main-sess")
    m = team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    return str(t["id"]), str(m.get("instance_id") or m.get("id"))


def _patch_member_turn(monkeypatch, running_lag: float, stream_hold: float = 0.0) -> None:
    """慢启动 worker：`_ensure_session` 慢 running_lag 秒（模拟 graph 构建
    /checkpoint IO 击穿旧 5s 轮询窗口），随后真实 runner 走到登记点；
    `_run_stream`（LLM 执行）桩成立即成功。dead=True 模拟真死信：session
    建不起来 → runner 早退，永不登记。"""

    # 真实 _ensure_session 是同步函数（graph 构建/checkpoint 读盘，同步
    # 阻塞）——生产的慢启动正来自这里的同步 IO 卡住事件循环；用
    # time.sleep 忠实复现该机制（而非 asyncio.sleep 的假慢）。
    def slow_ensure(sid):
        time.sleep(running_lag)
        return {
            "graph": object(),
            "project_slug": "default",
            "agent_id": "dev",
            "session_id": sid,
        }

    monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session", slow_ensure)

    async def fake_stream(ws, graph, config, text, session, agent_id, **k):
        await asyncio.sleep(stream_hold)  # hold>0：登记后长跑（模拟活 turn）
        return None

    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_stream)


async def test_slow_start_worker_not_falsely_stalled(member_iid, monkeypatch):
    """慢启动（前置 6.2s > 旧 5s 盲轮询窗口）不得误报 stalled。

    修复前：`_await_turn_start` 5s 轮询超时 → stalled（误报，turn 随后
    真的起来了）。修复后：ack 事件在登记瞬间（6.2s 处）到达，15s ack
    超时内确认 → delivered。
    """
    _patch_member_turn(monkeypatch, running_lag=6.2)
    r = await deliver_mod.deliver(member_iid[0], member_iid[1], "干活")
    assert r["receipt"] == "delivered", (
        f"慢启动 worker 被误报：{r}（ack 应在 15s 内到达，登记在 6.2s 处）"
    )
    assert r["ok"] is True


async def test_dead_worker_reports_stalled(member_iid, monkeypatch):
    """真死信语义保留：ack 永不发出（老版本 worker / ack 机制故障）时，
    超时兜底必须报 stalled——不能静默当成功。

    patch runner 的 notify 为 no-op（模拟不回 ack 的 worker）；turn 快进
    快出已结束 → 0.5s 超时后回查 `_is_live` 也不 live → stalled。
    超时阈值 `_ACK_TIMEOUT_S` 可配置（测试里 0.5s）。
    """
    _patch_member_turn(monkeypatch, running_lag=0.0)
    import ginno_runtime.subagent_scheduler.runner as runner_mod
    monkeypatch.setattr(runner_mod, "notify_turn_started", lambda sid: None)
    monkeypatch.setattr(deliver_mod, "_ACK_TIMEOUT_S", 0.5)
    r = await deliver_mod.deliver(member_iid[0], member_iid[1], "干活")
    assert r["receipt"] == "stalled"
    assert r["ok"] is False


async def test_no_ack_but_turn_live_not_falsely_stalled(member_iid, monkeypatch):
    """fail-safe 的另一半：ack 缺失但 turn 真在跑 → 超时回查 `_is_live`
    救回 delivered——老 worker 不回 ack 时不冤枉已起来的 turn（也不能
    把所有任务卡成假成功）。"""
    _patch_member_turn(monkeypatch, running_lag=0.0, stream_hold=2.0)
    import ginno_runtime.subagent_scheduler.runner as runner_mod
    monkeypatch.setattr(runner_mod, "notify_turn_started", lambda sid: None)
    monkeypatch.setattr(deliver_mod, "_ACK_TIMEOUT_S", 0.5)
    r = await deliver_mod.deliver(member_iid[0], member_iid[1], "干活")
    assert r["receipt"] == "delivered", (
        f"ack 缺失但 turn live，回查应救回：{r}"
    )


async def test_ack_resolves_wait_immediately(member_iid, monkeypatch):
    """ack 即解除等待：登记瞬间（1s 处）ack 到达即返回，不等满超时。"""
    _patch_member_turn(monkeypatch, running_lag=1.0)
    monkeypatch.setattr(deliver_mod, "_ACK_TIMEOUT_S", 60.0)  # 若在等超时必失败
    t0 = time.monotonic()
    r = await deliver_mod.deliver(member_iid[0], member_iid[1], "干活")
    elapsed = time.monotonic() - t0
    assert r["receipt"] == "delivered"
    assert elapsed < 10.0, f"ack 事件驱动应在 ~1s 返回，实际 {elapsed:.1f}s"


async def test_notify_turn_started_fires_ack(member_iid, monkeypatch):
    """ack 事件源钉住：`notify_turn_started` set 注册中的 future；无等待者
    时是 no-op（subagent/wake-parent 路径零行为变化）。"""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    server_shared._TURN_START_ACKS["sess-x"] = fut
    try:
        server_shared.notify_turn_started("sess-x")
        assert fut.done() and not fut.cancelled()
        # 无等待者：不抛
        server_shared.notify_turn_started("sess-nobody")
    finally:
        server_shared._TURN_START_ACKS.pop("sess-x", None)
        if not fut.done():
            fut.cancel()
