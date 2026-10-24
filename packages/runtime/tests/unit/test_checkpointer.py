"""Unit tests for the file-based LangGraph checkpointer (put / get_tuple round-trip)."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from ginno_runtime import jsonx as json
from ginno_runtime.checkpointer import FileCheckpointer, load_entries

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
    assert (sessions_dir / "s1.jsonl").is_file()


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
    # alist yields the history newest-first (design B P2: rerun_from walks it)
    tups = [t async for t in cp.alist(_cfg())]
    assert len(tups) == 1 and tups[0].checkpoint["id"] == checkpoint["id"]


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
    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / "s1.jsonl")
    modes = [c.get("mode") for c in raw]
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

    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / "s1.jsonl")
    assert [c.get("mode") for c in raw] == ["full", "full"]
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
        f = isolated_home / "projects" / slug / "sessions" / "big.jsonl"
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
    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / "s1.jsonl")
    assert all(c.get("mode") == "full" for c in raw)
    msgs = cp.get_tuple(_cfg()).checkpoint["channel_values"]["messages"]
    assert [m.id for m in msgs] == ["m1", "m2"]


async def test_delta_engages_in_real_graph_flow(isolated_home):
    """Regression: langgraph >= 1.x stopped embedding ``parent_config`` in the
    checkpoint dict, which silently forced every superstep to a full snapshot.
    Drive the REAL graph (no hand-injected parent_config) and assert deltas are
    stored + reconstruction is correct."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"checkpoint_mode": "delta"}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))

    model = ScriptedChatModel(scripts=[script(text=f"r{i}") for i in range(4)])
    graph = build_graph(
        model=model, project_slug="p", workspace="/tmp/ws", mcp_tools=[], all_tools=[]
    )
    cfg = {"configurable": {"thread_id": "real", "project_slug": "p"}}
    for i in range(4):
        await graph.ainvoke({"messages": [HumanMessage(content=f"q{i}")], "project_slug": "p"}, cfg)

    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / "real.jsonl")
    modes = [c.get("mode") for c in raw]
    # First checkpoint is full; subsequent supersteps must be deltas now that
    # the parent falls back to the last stored checkpoint.
    assert modes[0] == "full"
    assert "delta" in modes, f"delta mode never engaged: {modes}"

    # Reconstruction through the delta chain yields the full history.
    msgs = (await graph.aget_state(cfg)).values["messages"]
    humans = [m.content for m in msgs if isinstance(m, HumanMessage)]
    assert humans == ["q0", "q1", "q2", "q3"]
    assert any(isinstance(m, AIMessage) for m in msgs)
    assert not any(isinstance(m, ToolMessage) for m in msgs)


def test_delta_branch_degrades_to_full_not_corrupt(isolated_home):
    """Fallback assumes parent = last stored. On a branch/rewind the true
    parent is older, so the new history does NOT extend the last checkpoint —
    the pure-append check must reject the delta and store full (never link a
    delta onto a base it doesn't extend)."""
    settings = {"context": {"checkpoint_mode": "delta"}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    cp = FileCheckpointer(project_slug="p")

    m1 = HumanMessage(content="one", id="m1")
    m2 = HumanMessage(content="two", id="m2")
    m3 = HumanMessage(content="branch", id="m3")

    c1 = _checkpoint_with_messages(None, [m1])
    cp.put(_cfg(), c1, {"source": "input", "step": 0, "writes": {}}, {})
    c2 = _checkpoint_with_messages(None, [m1, m2])  # no parent_config at all
    cp.put(_cfg(), c2, {"source": "loop", "step": 1, "writes": {}}, {})

    # Branch off c1 ([m1, m3]) AFTER c2 was stored — does not extend [m1, m2].
    c3 = _checkpoint_with_messages(None, [m1, m3])
    cp.put(_cfg(), c3, {"source": "loop", "step": 2, "writes": {}}, {})

    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / "s1.jsonl")
    assert [c.get("mode") for c in raw] == ["full", "delta", "full"]
    # Latest reconstructs to the branch, not the abandoned linear path.
    msgs = cp.get_tuple(_cfg()).checkpoint["channel_values"]["messages"]
    assert [m.id for m in msgs] == ["m1", "m3"]


def test_put_writes_stored_without_exposing(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    cp.put(_cfg(), empty_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
    cp.put_writes(_cfg(), [("messages", HumanMessage(content="w", id="w1"))], "task-1")
    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / "s1.jsonl")
    assert raw[0].get("pending_writes"), "writes should be persisted"
    # behavior parity: get_tuple still exposes no pending writes
    tup = cp.get_tuple(_cfg())
    assert not getattr(tup, "pending_writes", None)


# --------------------------------------------------------------------------- #
# v2 — append-only JSONL log (file IO is O_APPEND, never a full rewrite)
# --------------------------------------------------------------------------- #
from ginno_runtime.checkpointer import clone_checkpoint  # noqa: E402


def _log(isolated_home, slug="p", sid="s1"):
    return isolated_home / "projects" / slug / "sessions" / f"{sid}.jsonl"


def _msg(text, mid):
    return HumanMessage(content=text, id=mid)


def test_commits_append_and_never_rewrite(isolated_home):
    """A put must leave every existing byte untouched (append-mode IO)."""
    cp = FileCheckpointer(project_slug="p")
    for i in range(2):
        cp.put(
            _cfg(),
            _checkpoint_with_messages(None, [_msg(f"m{i}", f"m{i}")]),
            {"source": "loop", "step": i, "writes": {}},
            {},
        )
    before = _log(isolated_home).read_bytes()
    cp.put(
        _cfg(),
        _checkpoint_with_messages(None, [_msg("m2", "m2")]),
        {"source": "loop", "step": 2, "writes": {}},
        {},
    )
    after = _log(isolated_home).read_bytes()
    assert after.startswith(before), "commit rewrote the log instead of appending"
    assert after.count(b"\n") == before.count(b"\n") + 1


def test_legacy_document_is_migrated_on_first_write(isolated_home):
    """v1 sessions stay readable; the first commit folds them into the log."""
    sessions = isolated_home / "projects" / "p" / "sessions"
    cp = FileCheckpointer(project_slug="p")
    cp.put(_cfg(), empty_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
    entries = load_entries(_log(isolated_home))
    _log(isolated_home).unlink()
    (sessions / "s1.json").write_text(
        json.dumps({"session_id": "s1", "checkpoints": entries})
    )

    # reads still serve the legacy document
    assert cp.get_tuple(_cfg()) is not None

    cp.put(_cfg(), empty_checkpoint(), {"source": "loop", "step": 1, "writes": {}}, {})
    assert _log(isolated_home).exists() and not (sessions / "s1.json").exists()
    assert len(load_entries(_log(isolated_home))) == 2
    assert cp.get_tuple(_cfg()) is not None


def test_truncate_appends_and_rewinds_head(isolated_home):
    cp = FileCheckpointer(project_slug="p", surface_pending_writes=True)
    ids: list[str] = []
    parent = None
    for i in range(3):
        parent = cp.put(
            _cfg(),
            _checkpoint_with_messages(parent, [_msg(f"m{i}", f"m{i}")]),
            {"source": "loop", "step": i, "writes": {}},
            {},
        )
        ids.append(parent["configurable"]["checkpoint_id"])
    cp.put_writes(
        {"configurable": {"thread_id": "s1", "checkpoint_id": ids[1]}},
        [("messages", _msg("w", "w1"))],
        "task-1",
    )

    before = _log(isolated_home).read_bytes()
    assert cp.truncate_to("s1", ids[1]) is True
    after = _log(isolated_home).read_bytes()
    assert after.startswith(before), "truncate rewrote the log instead of appending"

    entries = load_entries(_log(isolated_home))
    assert [c["checkpoint_id"] for c in entries] == ids[:2]
    assert not entries[1].get("pending_writes"), "head must be re-armed for re-execution"
    # head-is-target is a no-op (parity with the old rewrite)
    assert cp.truncate_to("s1", ids[1]) is False


def test_clone_checkpoint_retags_and_copies(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    cp.put(
        _cfg(),
        _checkpoint_with_messages(None, [_msg("m1", "m1")]),
        {"source": "input", "step": 0, "writes": {}},
        {},
    )
    dst = clone_checkpoint("p", "s1", "fork1")
    assert dst is not None and dst.name == "fork1.jsonl"
    assert [c["checkpoint_id"] for c in load_entries(dst)] == [
        c["checkpoint_id"] for c in load_entries(_log(isolated_home))
    ]
    header = json.loads(dst.read_text().splitlines()[0])
    assert header["op"] == "header" and header["session_id"] == "fork1"
    assert clone_checkpoint("p", "missing", "fork2") is None


def test_torn_tail_is_ignored_and_repaired(isolated_home):
    """A crash between write() and the trailing newline must not corrupt reads,
    and the next commit must not turn the torn line into interior corruption."""
    cp = FileCheckpointer(project_slug="p")
    cp.put(_cfg(), empty_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
    log = _log(isolated_home)
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"op":"cp","checkpoint_id":"torn')  # no newline, incomplete

    assert len(load_entries(log)) == 1  # trailing garbage ignored
    cp.put(_cfg(), empty_checkpoint(), {"source": "loop", "step": 1, "writes": {}}, {})
    assert len(load_entries(log)) == 2  # tail trimmed, not left mid-file
    assert cp.get_tuple(_cfg()) is not None


def test_self_referential_delta_is_rejected_not_hung(isolated_home):
    """A delta whose base is its own id must raise, never spin forever.

    (Crafting it directly: a re-put checkpoint dict reuses its parent's id.)"""
    cp = FileCheckpointer(project_slug="p")
    log = _log(isolated_home)
    log.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"op": "header", "v": 2, "session_id": "s1"},
        {"op": "cp", "checkpoint_id": "a", "parent_id": None, "mode": "delta",
         "base": "a", "static": {"type": "json", "data": "{}"}, "channels": {}},
    ]
    log.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(ValueError, match="cycle"):
        cp.get_tuple(_cfg())
