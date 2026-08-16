"""pending_writes surfacing + app-level progress channel (2026-08-17 follow-up).

LangGraph's standard mid-step resume relies on get_tuple returning the stored
put_writes; the workflow engine opts in via surface_pending_writes=True while
chat keeps the historical None. App-level channels (parallel_progress) must
stay invisible to pregel but readable via get_app_pending."""

from ginno_runtime.checkpointer import FileCheckpointer


def _cfg(thread_id="t1", checkpoint_id=None):
    c = {"configurable": {"thread_id": thread_id}}
    if checkpoint_id:
        c["configurable"]["checkpoint_id"] = checkpoint_id
    return c


def _checkpoint(cid):
    return {
        "id": cid,
        "ts": "2026-08-17T00:00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }


def _put(cp, cid):
    return cp.put(_cfg(), _checkpoint(cid), {"step": 1}, {})


def test_pending_writes_hidden_by_default_surfaced_with_flag(isolated_home):
    cp_off = FileCheckpointer("pw")
    ret = _put(cp_off, "cp1")
    cp_off.put_writes(ret, [("context", {"x": 1})], "task-1")
    assert cp_off.get_tuple(_cfg()) .pending_writes is None

    cp_on = FileCheckpointer("pw", surface_pending_writes=True)
    ret2 = _put(cp_on, "cp1")
    cp_on.put_writes(ret2, [("context", {"x": 1})], "task-1")
    tup = cp_on.get_tuple(_cfg())
    assert tup.pending_writes == [("task-1", "context", {"x": 1})]


def test_app_channel_filtered_from_pregel_but_readable(isolated_home):
    cp = FileCheckpointer("pw2", surface_pending_writes=True)
    ret = _put(cp, "cp1")
    cp.put_writes(ret, [("context", {"x": 1})], "task-1")
    cp.put_writes(ret, [("parallel_progress", {0: {"text": "t"}})], "body:gather")

    tup = cp.get_tuple(_cfg())
    # pregel never sees the app channel (it would skip the body node on resume)
    assert all(ch != "parallel_progress" for _, ch, _ in (tup.pending_writes or []))
    assert ("task-1", "context", {"x": 1}) in tup.pending_writes

    # ...but the node adapter can read it back
    assert cp.get_app_pending(_cfg(), "parallel_progress") == {0: {"text": "t"}}


def test_get_app_pending_last_write_wins(isolated_home):
    cp = FileCheckpointer("pw3", surface_pending_writes=True)
    ret = _put(cp, "cp1")
    cp.put_writes(ret, [("parallel_progress", {0: "a"})], "t")
    cp.put_writes(ret, [("parallel_progress", {0: "a", 1: "b"})], "t")
    assert cp.get_app_pending(_cfg(), "parallel_progress") == {0: "a", 1: "b"}
    # unknown app channels are refused
    assert cp.get_app_pending(_cfg(), "context") is None
