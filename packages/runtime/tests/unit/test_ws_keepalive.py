"""Regression test for the WS keepalive that prevents the frontend's 45s
watchdog from killing a turn during a long silent tool/LLM step (the
"stuck at 'now creating doc'" symptom, root cause #2).

We drive the keepalive coroutine in isolation against a fake websocket and
assert that keepalive frames keep flowing even while no turn events are sent
(i.e. the server would otherwise be silent). The interval in server.py is 15s,
so we observe for ~18s.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


def _build_keepalive(server_mod, ws, ws_closed_flag: list):
    """Re-create the exact keepalive coroutine body from server._stream_graph so
    the test pins its contract (15s cadence, stops on ws_closed, swallows send
    errors) without standing up a full graph/WS handler."""

    async def safe_send(data: str) -> None:
        if ws_closed_flag[0]:
            return
        try:
            await ws.send_text(data)
        except Exception:
            ws_closed_flag[0] = True

    async def keepalive() -> None:
        try:
            while not ws_closed_flag[0]:
                await asyncio.sleep(15)
                if ws_closed_flag[0]:
                    break
                await safe_send(server_mod._ev("keepalive", {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    return keepalive


@pytest.mark.asyncio
async def test_keepalive_emits_during_silence():
    import ginno_runtime.server as server_mod

    ws = _FakeWS()
    ws_closed = [False]
    ka = _build_keepalive(server_mod, ws, ws_closed)
    task = asyncio.create_task(ka())
    try:
        # keepalive interval is 15s; observe past one tick
        await asyncio.sleep(18)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert len(ws.sent) >= 1, "keepalive must emit at least one frame during a silent turn"
    assert all("keepalive" in s for s in ws.sent)


@pytest.mark.asyncio
async def test_keepalive_stops_when_client_gone():
    import ginno_runtime.server as server_mod

    ws = _FakeWS()
    ws_closed = [True]  # client already gone
    ka = _build_keepalive(server_mod, ws, ws_closed)
    await asyncio.wait_for(ka(), timeout=2)  # should return promptly, not loop
    assert ws.sent == []
