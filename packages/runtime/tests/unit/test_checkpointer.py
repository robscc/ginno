"""Unit tests for the file-based LangGraph checkpointer (put / get_tuple round-trip)."""

from __future__ import annotations

import json

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


# --------------------------------------------------------------------------- #
# E5 — delta (incremental) checkpoints
# --------------------------------------------------------------------------- #
from langchain_core.messages import HumanMessage  # noqa: E402

from ginno_runtime import paths  # noqa: E402


def _checkpoint_with_messages(parent_config, msgs):
    cp = empty_checkpoint()
    cp["channel_values"] = {"messages": list(msgs), "workspace": "/tmp/ws"}
    if parent_config:
        cp["parent_config"] = parent_config
    return cp


def test_delta_mode_stores_append_and_reconstructs(isolated_home):
    settings = {"context": {"checkpoint_mode": "delta"}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    cp = FileCheckpointer(project_slug="p")

    m1 = HumanMessage(content="one", id="m1")
    m2 = HumanMessage(content="two", id="m2")
    m3 = HumanMessage(content="three", id="m3")

    c1 = _checkpoint_with_messages(None, [m1])
    r1 = cp.put(_cfg(), c1, {"source": "input", "step": 0, "writes": {}}, {})
    c2 = _checkpoint_with_messages(r1, [m1, m2])
    r2 = cp.put(_cfg(), c2, {"source": "loop", "step": 1, "writes": {}}, {})
    c3 = _checkpoint_with_messages(r2, [m1, m2, m3])
    cp.put(_cfg(), c3, {"source": "loop", "step": 2, "writes": {}}, {})

    # reconstruction of the latest equals the full history
    tup = cp.get_tuple(_cfg())
    msgs = tup.checkpoint["channel_values"]["messages"]
    assert [m.id for m in msgs] == ["m1", "m2", "m3"]
    assert tup.checkpoint["channel_values"]["workspace"] == "/tmp/ws"

    # intermediate checkpoint reconstructs correctly too (time-travel)
    tup2 = cp.get_tuple(_cfg(checkpoint_id=r2["configurable"]["checkpoint_id"]))
    assert [m.id for m in tup2.checkpoint["channel_values"]["messages"]] == ["m1", "m2"]

    # and the file actually stores deltas, not three full copies
    raw = json.loads(
        (isolated_home / "projects" / "p" / "sessions" / "s1.json").read_text()
    )
    modes = [c.get("mode") for c in raw["checkpoints"]]
    assert modes == ["full", "delta", "delta"]


def test_delta_falls_back_to_full_on_rewrite(isolated_home):
    """Compaction replaces history → ids no longer extend the parent → full."""
    settings = {"context": {"checkpoint_mode": "delta"}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    cp = FileCheckpointer(project_slug="p")

    m1 = HumanMessage(content="one", id="m1")
    m2 = HumanMessage(content="two", id="m2")
    c1 = _checkpoint_with_messages(None, [m1, m2])
    r1 = cp.put(_cfg(), c1, {"source": "input", "step": 0, "writes": {}}, {})

    rewritten = HumanMessage(content="summary", id="s-new")
    c2 = _checkpoint_with_messages(r1, [rewritten])
    cp.put(_cfg(), c2, {"source": "loop", "step": 1, "writes": {}}, {})

    raw = json.loads(
        (isolated_home / "projects" / "p" / "sessions" / "s1.json").read_text()
    )
    assert [c.get("mode") for c in raw["checkpoints"]] == ["full", "full"]
    msgs = cp.get_tuple(_cfg()).checkpoint["channel_values"]["messages"]
    assert [m.id for m in msgs] == ["s-new"]


def test_delta_file_smaller_than_full(isolated_home):
    """Long appended history: delta file must beat N full snapshots."""
    import copy

    def run(mode: str) -> int:
        (isolated_home / "settings.json").write_text(
            json.dumps({"context": {"checkpoint_mode": mode}})
        )
        slug = f"p-{mode}"
        cp = FileCheckpointer(project_slug=slug)
        msgs: list = []
        parent = None
        for i in range(12):
            msgs = msgs + [HumanMessage(content=f"message {i} " + "x" * 200, id=f"m{i}")]
            c = _checkpoint_with_messages(parent, copy.deepcopy(msgs))
            parent = cp.put(
                _cfg("big"), c, {"source": "loop", "step": i, "writes": {}}, {}
            )
        f = isolated_home / "projects" / slug / "sessions" / "big.json"
        return f.stat().st_size

    full_size = run("full")
    delta_size = run("delta")
    assert delta_size < full_size * 0.5


def test_full_mode_setting(isolated_home):
    (isolated_home / "settings.json").write_text(
        json.dumps({"context": {"checkpoint_mode": "full"}})
    )
    cp = FileCheckpointer(project_slug="p")
    m1 = HumanMessage(content="one", id="m1")
    c1 = _checkpoint_with_messages(None, [m1])
    r1 = cp.put(_cfg(), c1, {"source": "input", "step": 0, "writes": {}}, {})
    c2 = _checkpoint_with_messages(r1, [m1, HumanMessage(content="two", id="m2")])
    cp.put(_cfg(), c2, {"source": "loop", "step": 1, "writes": {}}, {})
    raw = json.loads(
        (isolated_home / "projects" / "p" / "sessions" / "s1.json").read_text()
    )
    assert all(c.get("mode") == "full" for c in raw["checkpoints"])
    msgs = cp.get_tuple(_cfg()).checkpoint["channel_values"]["messages"]
    assert [m.id for m in msgs] == ["m1", "m2"]


def test_put_writes_stored_without_exposing(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    cp.put(_cfg(), empty_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
    cp.put_writes(_cfg(), [("messages", HumanMessage(content="w", id="w1"))], "task-1")
    raw = json.loads(
        (isolated_home / "projects" / "p" / "sessions" / "s1.json").read_text()
    )
    assert raw["checkpoints"][0].get("pending_writes"), "writes should be persisted"
    # behavior parity: get_tuple still exposes no pending writes
    tup = cp.get_tuple(_cfg())
    assert not getattr(tup, "pending_writes", None)
