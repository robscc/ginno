"""session_state 状态机单测：迁移/广播/派生/busy/idle 清理。"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import session_state


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    session_state._SESSION_STATE.clear()
    events = []

    async def fake_push(event, data):
        events.append((event, data))

    monkeypatch.setattr(
        "ginno_runtime.server_shared._push_global_event", fake_push
    )
    yield events


def test_default_idle():
    assert session_state.get_state("s1")["state"] == "idle"
    assert session_state.is_busy("s1") is False


def test_set_compacting_broadcasts(_clean):
    async def run():
        await session_state.set_state("s1", "compacting", detail="摘要中")
    asyncio.run(run())
    assert session_state.get_state("s1")["state"] == "compacting"
    assert session_state.is_busy("s1") is True
    assert _clean and _clean[0][0] == "session.state_changed"
    assert _clean[0][1]["state"] == "compacting"
    assert _clean[0][1]["prev"] == "idle"


def test_idle_removes_entry_and_broadcasts(_clean):
    async def run():
        await session_state.set_state("s1", "compacting")
        await session_state.set_state("s1", "idle")
    asyncio.run(run())
    assert "s1" not in session_state._SESSION_STATE
    assert session_state.get_state("s1")["state"] == "idle"
    assert len(_clean) == 2
    assert _clean[1][1]["prev"] == "compacting"


def test_same_state_no_broadcast(_clean):
    async def run():
        await session_state.set_state("s1", "compacting")
        await session_state.set_state("s1", "compacting")
    asyncio.run(run())
    assert len(_clean) == 1  # 重复迁移不广播


def test_bad_state_raises():
    with pytest.raises(ValueError):
        asyncio.run(session_state.set_state("s1", "bogus"))


def test_derive_running_from_registry(monkeypatch):
    from ginno_runtime import server_shared

    monkeypatch.setitem(server_shared._RUNNING_TURNS, "s2", "t1")
    assert session_state.derive_state("s2") == "running"
    assert session_state.is_busy("s2") is True


def test_derive_paused_from_pending(monkeypatch):
    from ginno_runtime import server_shared

    monkeypatch.setattr(server_shared, "_PENDING_RESUME", {"s3"})
    assert session_state.derive_state("s3") == "paused"


def test_compacting_overrides_registry(monkeypatch):
    from ginno_runtime import server_shared

    monkeypatch.setitem(server_shared._RUNNING_TURNS, "s4", "t1")
    asyncio.run(session_state.set_state("s4", "compacting"))
    # 显式 compacting 优先于注册表 running
    assert session_state.derive_state("s4") == "compacting"
