"""Tests for limited history reads through the real file checkpoint saver."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from ginno_runtime import checkpointer as ck
from ginno_runtime import jsonx as json

pytestmark = pytest.mark.unit


def _config(checkpoint_id=None, **options):
    if checkpoint_id is not None:
        options["checkpoint_id"] = checkpoint_id
    return {"configurable": {"thread_id": "s1", **options}}


def _put_history(saver, isolated_home, mode="delta", *, anchor_size=64):
    (isolated_home / "settings.json").write_text(
        json.dumps({"context": {"checkpoint_mode": mode}}), encoding="utf-8"
    )
    config = _config()
    messages = [HumanMessage(content="锚" + "x" * anchor_size, id="anchor")]
    for step in range(8):
        checkpoint = empty_checkpoint()
        # Non-monotonic ids make the expected file order distinct from id sorting.
        checkpoint["id"] = f"cp-{8 - step}"
        checkpoint["channel_values"] = {"messages": messages.copy()}
        config = saver.put(config, checkpoint, {"source": "loop", "step": step}, {})
        messages.append(HumanMessage(content=f"追加 {step}", id=f"msg-{step}"))
    return config


def _log_reads(monkeypatch, log):
    reads = []
    read_text = Path.read_text

    def counted(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path == log:
            reads.append(len(text.encode("utf-8")))
        return text

    monkeypatch.setattr(Path, "read_text", counted)
    return reads


@pytest.mark.parametrize("mode", ["full", "delta"])
async def test_limited_list_reuses_verified_suffix(isolated_home, monkeypatch, mode):
    monkeypatch.setattr(ck, "_LIST_SUFFIX_BYTES", 32 * 1024, raising=False)
    saver = ck.FileCheckpointer("p", surface_pending_writes=True)
    old = empty_checkpoint()
    old["channel_values"] = {
        "messages": [HumanMessage(content="x" * (64 * 1024), id="old")],
    }
    saver.put(_config(), old, {"step": -1}, {})
    head = _put_history(saver, isolated_home, mode)
    saver.put_writes(head, [("visible", {"a": [1]}), ("parallel_progress", "internal")], "task")
    log = ck.checkpoint_path("p", "s1")
    entries = ck.load_entries(log)
    assert [e["mode"] for e in entries] == ["full", "full"] + [mode] * 7
    expected = list(saver.list(_config()))[:3]
    reads = _log_reads(monkeypatch, log)

    first = list(saver.list(_config(), limit=3))
    assert first == expected
    assert reads == [log.stat().st_size]
    first[0].checkpoint["channel_values"]["messages"].clear()
    first[0].config["configurable"]["checkpoint_id"] = "caller mutation"
    first[0].pending_writes[0][2]["a"].clear()

    other_instance = ck.FileCheckpointer("p", surface_pending_writes=True)
    assert [t async for t in other_instance.alist(_config(), limit=3)] == expected
    assert expected[0].pending_writes == [("task", "visible", {"a": [1]})]
    assert list(ck.FileCheckpointer("p").list(_config(), limit=1))[0].pending_writes is None
    assert reads == [log.stat().st_size]


def test_limited_list_queries_keep_existing_order_and_boundary(isolated_home):
    saver = ck.FileCheckpointer("p")
    _put_history(saver, isolated_home)
    full = list(saver.list(_config()))
    assert [t.metadata["step"] for t in full] == list(reversed(range(8)))
    assert list(saver.list(_config(), limit=5)) == full[:5]

    # FileCheckpointer has thread-only storage and currently accepts metadata
    # filter and checkpoint_ns without applying them.
    config = _config(checkpoint_ns="child", checkpoint_id="ignored-by-list")
    criteria = {"source": "input", "step": 4}
    assert list(saver.list(config, filter=criteria, limit=5)) == full[:5]
    before = full[2].config
    assert list(saver.list(config, before=before, filter=criteria, limit=5)) == full[:2]
    assert list(saver.list(config, before={"checkpoint_id": "unknown"}, limit=5)) == full[:5]
    assert list(saver.list(config, before=full[0].config, limit=1)) == []
    assert list(saver.list(config, limit=99)) == full
    for limit in (0, -1):
        assert list(saver.list(config, limit=limit)) == full[:1]


def test_limited_list_falls_back_for_delta_anchor_outside_cache(isolated_home, monkeypatch):
    monkeypatch.setattr(ck, "_LIST_SUFFIX_BYTES", 16 * 1024)
    saver = ck.FileCheckpointer("p")
    _put_history(saver, isolated_home, anchor_size=64 * 1024)
    log = ck.checkpoint_path("p", "s1")
    expected = list(saver.list(_config()))[:3]
    assert [len(t.checkpoint["channel_values"]["messages"]) for t in expected] == [8, 7, 6]
    reads = _log_reads(monkeypatch, log)

    assert list(saver.list(_config(), limit=3)) == expected
    assert list(saver.list(_config(), limit=3)) == expected
    assert reads == [log.stat().st_size] * 2


def test_limited_list_skips_broken_entries_and_counts_successes(isolated_home):
    saver = ck.FileCheckpointer("p")
    _put_history(saver, isolated_home, mode="full")
    log = ck.checkpoint_path("p", "s1")
    entries = ck.load_entries(log)
    entries[-1].pop("checkpoint")
    entries[-1].update(mode="delta", base="missing")
    entries[-2]["metadata"] = {"type": "msgpack", "data": "!"}
    ck._write_events_atomic(log, [ck._header_event("s1"), *({**e, "op": "cp"} for e in entries)])

    for _ in range(2):
        actual = list(saver.list(_config(), limit=3))
        assert [t.metadata["step"] for t in actual] == [5, 4, 3]


def test_limited_list_revalidates_after_writes_and_rewind(isolated_home):
    reader = ck.FileCheckpointer("p", surface_pending_writes=True)
    head = _put_history(reader, isolated_home)
    original = list(reader.list(_config(), limit=3))
    assert original[0].pending_writes is None

    def append_write(index):
        ck.FileCheckpointer("p").put_writes(head, [("result", index)], f"task-{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append_write, range(8)))
    result = list(reader.list(_config(), limit=3))
    assert [t.config for t in result] == [t.config for t in original]
    assert sorted(result[0].pending_writes) == [(f"task-{i}", "result", i) for i in range(8)]
    assert list(reader.list(_config(), limit=3)) == result

    target = original[1]
    reader.put_writes(target.config, [("result", 42)], "rewind-task")
    assert list(reader.list(_config(), limit=3))[1].pending_writes == [("rewind-task", "result", 42)]
    assert reader.truncate_to("s1", target.checkpoint["id"])
    after = list(reader.list(_config(), limit=3))
    assert after[0] == target
    assert [t.metadata["step"] for t in after] == [6, 5, 4]


def test_limited_list_does_not_publish_snapshot_racing_append(isolated_home, monkeypatch):
    reader = ck.FileCheckpointer("p", surface_pending_writes=True)
    writer = ck.FileCheckpointer("p")
    head = _put_history(reader, isolated_home)
    log = ck.checkpoint_path("p", "s1")
    read_text = Path.read_text
    appended = False

    with ThreadPoolExecutor(max_workers=1) as pool:
        def append_after_read(path, *args, **kwargs):
            nonlocal appended
            text = read_text(path, *args, **kwargs)
            if path == log and not appended:
                appended = True
                pool.submit(writer.put_writes, head, [("result", 42)], "task").result(timeout=5)
            return text

        monkeypatch.setattr(Path, "read_text", append_after_read)
        assert list(reader.list(_config(), limit=3))[0].pending_writes is None
        assert list(reader.list(_config(), limit=3))[0].pending_writes == [("task", "result", 42)]


def test_limited_list_torn_tail_and_interior_corruption(isolated_home):
    saver = ck.FileCheckpointer("p", surface_pending_writes=True)
    head = _put_history(saver, isolated_home)
    log = ck.checkpoint_path("p", "s1")
    expected = list(saver.list(_config(), limit=3))
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"op":"cp","checkpoint_id":"unfinished')
    assert list(saver.list(_config(), limit=3)) == expected

    saver.put_writes(head, [("result", 42)], "task")
    assert list(saver.list(_config(), limit=3))[0].pending_writes == [("task", "result", 42)]
    data = log.read_bytes()
    first_checkpoint = data.index(b"\n") + 1
    assert data[first_checkpoint:first_checkpoint + 1] == b"{"
    old_stat = log.stat()
    log.write_bytes(data[:first_checkpoint] + b"!" + data[first_checkpoint + 1:])
    os.utime(log, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(ValueError, match="unparsable line 2"):
        list(saver.list(_config(), limit=3))


def test_limited_list_legacy_json_and_migration(isolated_home):
    saver = ck.FileCheckpointer("p")
    head = _put_history(saver, isolated_home)
    log = ck.checkpoint_path("p", "s1")
    expected = list(saver.list(_config()))
    legacy = ck.legacy_checkpoint_path("p", "s1")
    legacy.write_text(json.dumps({"checkpoints": ck.load_entries(log)}), encoding="utf-8")
    log.unlink()

    assert list(saver.list(_config(), limit=3)) == expected[:3]
    saver.put_writes(head, [("result", 42)], "task")
    assert log.is_file()
    assert list(saver.list(_config(), limit=3)) == expected[:3]


def test_limited_list_rereads_referenced_archive(isolated_home):
    saver = ck.FileCheckpointer("p")
    _put_history(saver, isolated_home)
    log = ck.checkpoint_path("p", "s1")
    expected = list(saver.list(_config()))[:5]
    assert list(saver.list(_config(), limit=5)) == expected
    entries = [{**e, "op": "cp"} for e in ck.load_entries(log)]
    archive = log.with_name("s1.seg.1.jsonl")
    ck._write_events_atomic(archive, entries[:4])
    ck._write_events_atomic(log, [
        ck._header_event("s1"), {"op": "seg", "archives": [archive.name]}, *entries[4:]
    ])
    assert list(saver.list(_config(), limit=5)) == expected

    entries[3]["metadata"] = ck._dump_typed(saver.serde.dumps_typed({"step": 42}))
    ck._write_events_atomic(archive, entries[:4])
    actual = list(saver.list(_config(), limit=5))
    assert actual[:-1] == expected[:-1]
    assert actual[-1].checkpoint == expected[-1].checkpoint
    assert actual[-1].metadata == {"step": 42}
