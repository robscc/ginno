"""E2E test: memory capture after a turn (pool count via GET /memory)."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def test_capture_after_turn(client, create_session, ws_conv):
    """After a turn with text output, the pool should have the captured text."""
    sid = create_session([script(text="the assistant reply text")])
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")

    # pool should have 1 entry (the captured assistant text)
    mem = client.get("/memory").json()
    assert mem["ok"] is True
    assert mem["pool_count"] >= 1
