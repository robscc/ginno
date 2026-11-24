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
from unittest.mock import Mock

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


@pytest.fixture
async def member_turn(monkeypatch):
    """Run the real member runner with isolated session and stream I/O."""
    from ginno_runtime.subagent_scheduler import runner

    release_stream = asyncio.Event()
    release_stream.set()
    tasks = []

    def spawn(coro):
        task = server_shared.spawn_bg(coro)
        tasks.append(task)
        return task

    ensure = Mock(side_effect=lambda sid: {
        "graph": object(), "project_slug": "default", "agent_id": "dev", "session_id": sid,
    })

    async def fake_stream(*args, **kwargs):
        await release_stream.wait()

    monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session", ensure)
    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_stream)
    monkeypatch.setattr(deliver_mod, "spawn_bg", spawn)
    notify = Mock(wraps=runner.notify_turn_started)
    monkeypatch.setattr(runner, "notify_turn_started", notify)
    try:
        yield ensure, notify, release_stream, tasks
    finally:
        release_stream.set()
        await asyncio.gather(*tasks)


async def test_slow_start_worker_not_falsely_stalled(member_iid, member_turn, monkeypatch):
    """Advance past the old 5s window at runner lookup, inside the 15s ack window."""
    ensure, notify, _, tasks = member_turn
    loop = asyncio.get_running_loop()
    real_time = loop.time
    elapsed = 0.0
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: real_time() + elapsed)
        ensure_session = ensure.side_effect

        def slow_ensure(sid):
            nonlocal elapsed
            # deliver preflights the session before spawning. Only the runner's
            # second lookup models the slow setup inside the ack window.
            if ensure.call_count == 2:
                assert sid in server_shared._TURN_START_ACKS
                elapsed += 6.2
            return ensure_session(sid)

        ensure.side_effect = slow_ensure
        r = await asyncio.wait_for(deliver_mod.deliver(*member_iid, "干活"), timeout=8)
    assert ensure.call_count == 2
    notify.assert_called_once_with("sess-dev")
    assert tasks[0].done()
    assert "sess-dev" not in server_shared._RUNNING_TURNS
    assert r["receipt"] == "delivered"
    assert r["ok"] is True


async def test_dead_worker_reports_stalled(member_iid, member_turn, monkeypatch):
    """No ack and the turn already finished when the fallback checks liveness."""
    _, notify, _, tasks = member_turn
    notify.side_effect = lambda sid: None
    monkeypatch.setattr(deliver_mod, "_ACK_TIMEOUT_S", 0.1)
    r = await deliver_mod.deliver(*member_iid, "干活")
    notify.assert_called_once_with("sess-dev")
    assert tasks[0].done()
    assert r["receipt"] == "stalled"
    assert r["ok"] is False


async def test_no_ack_but_turn_live_not_falsely_stalled(member_iid, member_turn, monkeypatch):
    """No ack but the runner stays live until the fallback returns."""
    _, notify, release_stream, tasks = member_turn
    notify.side_effect = lambda sid: None
    release_stream.clear()
    monkeypatch.setattr(deliver_mod, "_ACK_TIMEOUT_S", 0.1)
    r = await deliver_mod.deliver(*member_iid, "干活")
    notify.assert_called_once_with("sess-dev")
    assert not tasks[0].done()
    assert r["receipt"] == "delivered"


async def test_ack_resolves_wait_immediately(member_iid, member_turn, monkeypatch):
    """Delivery resolves on ack while the stream is still blocked."""
    _, notify, release_stream, tasks = member_turn
    release_stream.clear()
    monkeypatch.setattr(deliver_mod, "_ACK_TIMEOUT_S", 60.0)
    r = await asyncio.wait_for(deliver_mod.deliver(*member_iid, "干活"), timeout=2)
    notify.assert_called_once_with("sess-dev")
    assert not tasks[0].done()
    assert r["receipt"] == "delivered"


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
