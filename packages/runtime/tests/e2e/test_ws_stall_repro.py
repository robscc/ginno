"""Reproduction + root-cause probe for "long task hangs / tool status disappears".

Observed in the wild:
  (A) client's 45s watchdog self-disconnect during a silent stretch;
  (B) gateway/SDK read stall (~600s): server emits nothing, no tool events,
      turn "hangs" (the 7m49s turn).

We reproduce a *gateway-style async stall* with ``StallModel``: stream one token,
``await asyncio.sleep(STALL_S)`` (an async wait that YIELDS the event loop —
exactly like a stuck network read; NOT a blocking time.sleep, which would starve
the keepalive task), then stream another token.

Starlette's TestClient WS has no timeout-read API, so we drain frames in a
background thread (receive_json blocks in the portal thread) into a queue the
main thread reads WITH A TIMEOUT. That lets us SEE what the server emits during
the silent stall window.

What the result proves (the diagnosis):
  * keepalive frames DO flow during the async stall  -> server keepalive works,
    so the client 45s watchdog (mode A) would NOT fire;
  * yet the turn still takes the FULL stall duration -> keepalive alone does NOT
    cure a gateway stall (mode B); the server needs a model read-timeout to fail
    fast instead of waiting the whole stall.
  (Note: a *blocking* stall, e.g. a sync tool doing time.sleep, would ALSO starve
  keepalive — a separate footgun; this test uses the async stall that matches a
  stuck gateway.)
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from typing import Any, AsyncIterator

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk

from ginno_runtime import server
from ginno_runtime import server as server_mod

pytestmark = pytest.mark.e2e

STALL_S = 20  # > keepalive interval (15s) so >=1 keepalive fires during the stall


class StallModel(BaseChatModel):
    """Async-stall model: token -> async sleep (gateway hang) -> token."""

    @property
    def _llm_type(self) -> str:
        return "stall"

    def bind_tools(self, *a, **k):
        return self

    async def _astream(self, *a, **k) -> AsyncIterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=AIMessageChunk(content="before-stall "))
        await asyncio.sleep(STALL_S)  # yields the loop -> keepalive can run
        yield ChatGenerationChunk(message=AIMessageChunk(content="after-stall"))

    def _stream(self, *a, **k):  # pragma: no cover - sync fallback
        yield ChatGenerationChunk(message=AIMessageChunk(content="x"))

    def _generate(self, *a, **k):
        return ChatGeneration(message=AIMessage(content="x"))


def _drain(ws, q: queue.Queue, stop: threading.Event):
    while not stop.is_set():
        try:
            q.put(ws.receive_json())
        except Exception:
            q.put(None)
            return


def _run_and_collect(client, sid):
    """Invoke + collect timestamped frames via a background reader thread."""
    frames: list[tuple[float, str]] = []
    with client.websocket_connect(f"/api/ws/sessions/{sid}") as ws:
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        rdr = threading.Thread(target=_drain, args=(ws, q, stop), daemon=True)
        rdr.start()
        ws.send_json({"type": "invoke", "message": "go", "turn_id": "stall-probe"})
        t0 = time.time()
        try:
            while time.time() - t0 < STALL_S + 15:
                try:
                    ev = q.get(timeout=3)
                except queue.Empty:
                    continue
                if ev is None:
                    break
                frames.append((time.time() - t0, ev.get("event")))
                if ev.get("event") in ("message.end", "error"):
                    break
        finally:
            stop.set()
    return frames


def test_keepalive_flows_during_stall(client, patch_build_model, monkeypatch):
    """Mode-A protection: with the stall watchdog disabled (set above the stall),
    keepalive frames MUST flow during the silent stall, so the client's 45s
    watchdog would NOT fire."""
    patch_build_model(StallModel())
    monkeypatch.setattr("ginno_runtime.api.stream.CHUNK_TIMEOUT_S", STALL_S + 30)  # watchdog off
    sid = client.post(
        "/api/sessions", json={"project_slug": "default", "workspace": "/tmp/wf-ws"}
    ).json()["id"]
    kinds = [(round(t, 1), k) for t, k in _run_and_collect(client, sid)]
    keepalives = [t for t, k in kinds if k == "keepalive"]
    token_times = [t for t, k in kinds if k == "token.delta"]
    end = [t for t, k in kinds if k in ("message.end", "error")]
    assert keepalives, f"no keepalive during stall; frames={kinds}"
    assert any(13 < t < STALL_S + 2 for t in keepalives), f"keepalive not in stall window: {keepalives}"
    assert token_times and token_times[0] < 3, f"first token not immediate: {token_times}"
    # without the watchdog the turn waits out the whole stall (proves keepalive
    # alone does NOT cure a gateway stall -> we need the watchdog too)
    assert end and end[0] >= STALL_S - 1, f"turn did not wait out the stall: {end}"


def test_stall_watchdog_fails_fast(client, patch_build_model, monkeypatch):
    """Mode-B fix: the per-chunk stall watchdog aborts a silent stall FAST with an
    `error` event, instead of the SDK's ~600s hang (the 7m49s symptom)."""
    patch_build_model(StallModel())
    monkeypatch.setattr("ginno_runtime.api.stream.CHUNK_TIMEOUT_S", 3.0)  # fires during the 20s stall
    sid = client.post(
        "/api/sessions", json={"project_slug": "default", "workspace": "/tmp/wf-ws"}
    ).json()["id"]
    kinds = [(round(t, 1), k) for t, k in _run_and_collect(client, sid)]
    end = [(t, k) for t, k in kinds if k in ("message.end", "error")]
    token_times = [t for t, k in kinds if k == "token.delta"]
    assert end, f"turn never terminated; frames={kinds}"
    end_t, end_k = end[0]
    assert end_k == "error", f"stall should surface an error, got {end_k}; frames={kinds}"
    assert end_t < 12, f"turn hung {end_t}s (watchdog did not fire); frames={kinds}"
    assert token_times and token_times[0] < 3, f"first token not immediate: {token_times}"
