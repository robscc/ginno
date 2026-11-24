"""Exercise the turn's real 15-second keepalive without waiting on wall time."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ginno_runtime import server_shared
from ginno_runtime.api.stream.stages import streamio

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("connected", [True, False], ids=["silent", "disconnected"])
async def test_keepalive_ticks_until_cancelled(monkeypatch, connected):
    ticks = asyncio.Queue()
    release = asyncio.Event()

    async def sleep(delay):
        ticks.put_nowait(delay)
        await release.wait()
        release.clear()

    monkeypatch.setattr(streamio, "asyncio", SimpleNamespace(
        sleep=sleep, CancelledError=asyncio.CancelledError,
    ))
    ws = SimpleNamespace(send_text=AsyncMock())
    ctx = SimpleNamespace(session_id="silent-turn", emit=server_shared._ev)
    ctx.safe_send = streamio.make_safe_send(ctx)
    monkeypatch.setitem(streamio.eng._SESSION_WS, ctx.session_id, [ws] if connected else [])

    task = asyncio.create_task(streamio.keepalive(ctx))
    try:
        for tick in range(2):
            assert await asyncio.wait_for(ticks.get(), timeout=2) == 15
            assert ws.send_text.await_count == (tick if connected else 0)
            release.set()
        assert await asyncio.wait_for(ticks.get(), timeout=2) == 15
        assert ctx.ws_closed is not connected
        assert [json.loads(call.args[0]) for call in ws.send_text.await_args_list] == (
            [{"event": "keepalive"}] * 2 if connected else []
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
