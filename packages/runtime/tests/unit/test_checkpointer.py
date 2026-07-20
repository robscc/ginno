"""Unit tests for the file-based LangGraph checkpointer (put / get_tuple round-trip)."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from ginno_runtime.checkpointer import FileCheckpointer

pytestmark = pytest.mark.unit


def _cfg(thread_id="s1", checkpoint_id=None):
    c = {"configurable": {"thread_id": thread_id}}
    if checkpoint_id:
        c["configurable"]["checkpoint_id"] = checkpoint_id
    return c


def test_get_tuple_empty_returns_none(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    assert cp.get_tuple(_cfg()) is None


def test_put_then_get_roundtrip(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    checkpoint = empty_checkpoint()
    cp.put(_cfg(), checkpoint, {"source": "input", "step": 0, "writes": {}}, {})
    tup = cp.get_tuple(_cfg())
    assert tup is not None
    assert tup.checkpoint["id"] == checkpoint["id"]
    assert tup.metadata["source"] == "input"


def test_get_latest_when_no_checkpoint_id(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    first = empty_checkpoint()
    second = empty_checkpoint()
    cp.put(_cfg(), first, {"source": "input", "step": 0, "writes": {}}, {})
    cp.put(_cfg(), second, {"source": "loop", "step": 1, "writes": {}}, {})
    assert cp.get_tuple(_cfg()).checkpoint["id"] == second["id"]


def test_get_specific_checkpoint_id(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    first = empty_checkpoint()
    second = empty_checkpoint()
    cp.put(_cfg(), first, {"source": "input", "step": 0, "writes": {}}, {})
    cp.put(_cfg(), second, {"source": "loop", "step": 1, "writes": {}}, {})
    tup = cp.get_tuple(_cfg(checkpoint_id=first["id"]))
    assert tup.checkpoint["id"] == first["id"]


def test_atomic_write_leaves_no_tmp(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    cp.put(_cfg(), empty_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
    sessions_dir = isolated_home / "projects" / "p" / "sessions"
    assert list(sessions_dir.glob("*.tmp")) == []
    assert (sessions_dir / "s1.json").is_file()


def test_sessions_are_isolated_per_thread(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    a = empty_checkpoint()
    b = empty_checkpoint()
    cp.put(_cfg("A"), a, {"source": "input", "step": 0, "writes": {}}, {})
    cp.put(_cfg("B"), b, {"source": "input", "step": 0, "writes": {}}, {})
    assert cp.get_tuple(_cfg("A")).checkpoint["id"] == a["id"]
    assert cp.get_tuple(_cfg("B")).checkpoint["id"] == b["id"]


def test_noop_writes_and_list(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    assert cp.put_writes(_cfg(), [], "task") is None
    assert list(cp.list(_cfg())) == []


async def test_async_put_and_get(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    checkpoint = empty_checkpoint()
    await cp.aput(_cfg(), checkpoint, {"source": "input", "step": 0, "writes": {}}, {})
    tup = await cp.aget_tuple(_cfg())
    assert tup.checkpoint["id"] == checkpoint["id"]
    assert list(await cp.alist(_cfg())) == []
