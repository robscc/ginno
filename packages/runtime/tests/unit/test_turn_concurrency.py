"""Issue #14 — 同 session 多生产者并发：channel 入站必须与 WS invoke 共享
`_turn_lock(session_id)`，两个 turn 不得在同一 session 上交叠执行。
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import server_shared as shared
from ginno_runtime.channels import base as ch_base

pytestmark = pytest.mark.unit


class _FakeTap:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def close(self) -> None:
        pass


def _session(sid: str) -> dict:
    return {"session_id": sid, "project_slug": "p", "graph": None, "agent_id": None}


async def test_channel_turn_holds_turn_lock(isolated_home, monkeypatch):
    """run_channel_turn 内部执行 _run_stream 时必须持有 session 的 turn lock
    （与桌面 WS invoke 路径同契约）。"""
    from ginno_runtime.api import stream as stream_api

    observed: list[bool] = []

    async def fake_run_stream(*args, **kwargs):
        observed.append(shared._turn_lock("sess1").locked())

    monkeypatch.setattr(stream_api, "_run_stream", fake_run_stream)

    await ch_base.run_channel_turn(_session("sess1"), "hello", _FakeTap("sess1"))
    assert observed == [True]
    # turn 结束后锁必须释放
    assert not shared._turn_lock("sess1").locked()


async def test_concurrent_channel_turns_serialize(isolated_home, monkeypatch):
    """两条渠道消息同时到达同一 session：不得并发执行（后到者排队）。"""
    from ginno_runtime.api import stream as stream_api

    state = {"active": 0, "peak": 0}

    async def fake_run_stream(*args, **kwargs):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1

    monkeypatch.setattr(stream_api, "_run_stream", fake_run_stream)

    await asyncio.gather(
        ch_base.run_channel_turn(_session("sess2"), "one", _FakeTap("sess2")),
        ch_base.run_channel_turn(_session("sess2"), "two", _FakeTap("sess2")),
    )
    assert state["peak"] == 1


async def test_ws_invoke_and_channel_turn_serialize(isolated_home, monkeypatch):
    """WS invoke 先持锁（模拟长 turn），channel 入站必须等它结束才开始——
    而不是并行跑第二个 graph.astream。"""
    from ginno_runtime.api import stream as stream_api

    state = {"active": 0, "peak": 0}

    async def fake_run_stream(*args, **kwargs):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1

    monkeypatch.setattr(stream_api, "_run_stream", fake_run_stream)

    lock = shared._turn_lock("sess3")

    async def ws_invoke():
        async with lock:
            await fake_run_stream()

    await asyncio.gather(
        ws_invoke(),
        ch_base.run_channel_turn(_session("sess3"), "from channel", _FakeTap("sess3")),
    )
    assert state["peak"] == 1
