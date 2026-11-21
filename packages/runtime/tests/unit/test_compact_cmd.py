"""Unit tests for /compact — the failure path must emit exactly ONE done event.

P3 audit fix (2026-11): the ``except`` branch used to ``await _announce_done()``
and then ``return``, while the outer ``finally`` fired the same event again —
two ``context.compacting {stage: done}`` per failed /compact. The contract is
"exactly one terminal event" (the frontend unlocks the Composer on it); the
fix keeps only the ``finally`` (always reached, even on the return path).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from ginno_runtime.commands.session_cmds import _compact_async_handler

pytestmark = pytest.mark.unit


class _FakeGraph:
    """Minimal graph stand-in: one snapshot, no live turn."""

    def __init__(self, msgs):
        self._snap = SimpleNamespace(values={"messages": msgs}, next=None)

    async def aget_state(self, config):
        return self._snap


async def test_compact_failure_done_event_sent_exactly_once(monkeypatch, isolated_home):
    events: list[tuple[str, dict | None]] = []

    async def fake_push_session_event(sid, event, payload=None):
        events.append((event, payload))

    # Handlers import these lazily inside the function body → patch the source
    # module attributes; each late import picks up the patched reference.
    monkeypatch.setattr(
        "ginno_runtime.server_shared._push_session_event", fake_push_session_event
    )
    monkeypatch.setattr(
        "ginno_runtime.compaction.maybe_compact_history",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(
        "ginno_runtime.microcompact.maybe_microcompact_history",
        AsyncMock(return_value=None),
    )

    graph = _FakeGraph([HumanMessage(content="q0", id="h0")])
    session = {"session_id": "cc-done-1", "graph": graph, "project_slug": "default"}
    out = await _compact_async_handler(session=session)

    assert "Compaction failed" in out or "压缩失败" in out
    done = [
        payload
        for event, payload in events
        if event == "context.compacting" and (payload or {}).get("stage") == "done"
    ]
    assert len(done) == 1  # pre-fix this was 2 (except-branch + finally)
