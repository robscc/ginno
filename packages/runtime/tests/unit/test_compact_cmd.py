"""A failed /compact ends its active status exactly once."""

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

    async def fake_push_session_event(sid, event, payload, turn_id):
        events.append((event, payload))

    # Handlers import these lazily inside the function body → patch the source
    # module attributes; each late import picks up the patched reference.
    monkeypatch.setattr(
        "ginno_runtime.server_shared._push_session_event", fake_push_session_event
    )
    monkeypatch.setattr(
        "ginno_runtime.world_state.context_settings",
        lambda: {"compact_keep_turns": 1},
    )
    monkeypatch.setattr(
        "ginno_runtime.microcompact.maybe_microcompact_history",
        AsyncMock(return_value=None),
    )

    graph = _FakeGraph([HumanMessage(content=f"q{i}", id=f"h{i}") for i in range(2)])
    session = {
        "session_id": "cc-done-1", "graph": graph, "project_slug": "default",
        "model": SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("boom"))),
    }
    out = await _compact_async_handler(session=session)

    assert "Compaction failed" in out or "压缩失败" in out
    assert events == [
        ("context.compacting", {"stage": "summarizing"}),
        ("context.compacting", {"stage": "done"}),
    ]
