"""Regressions for recovery claims, snapshot writes and restart receipts."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph.message import add_messages

from ginno_runtime import lastwords, server_shared as shared
from ginno_runtime.api import sessions, stream
from ginno_runtime.api.stream import remote_turn

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolate_recovery(monkeypatch):
    for name in ("_PENDING_RESUME", "_REMOTE_HEADS", "_LAST_WRITE_AT", "_EFFORT"):
        monkeypatch.setattr(lastwords, name, {})
    monkeypatch.setattr(lastwords, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(shared, "_TURN_LOCKS", {})


def test_late_prepare_cannot_recreate_finished_snapshot(monkeypatch):
    sid, tid = "prepare", "original"
    messages = [HumanMessage(content="Current", id=tid)]
    shared._RUNNING_TURNS[sid] = tid
    monkeypatch.setattr(lastwords, "_resolve_slug", lambda _: "default")
    lastwords.note_turn_start(sid, tid, "default", input_messages=messages)
    loading, resume = Event(), Event()

    def load(*_):
        loading.set()
        assert resume.wait(5)
        return messages

    with ThreadPoolExecutor(max_workers=1) as pool:
        write = pool.submit(lastwords.write_lastwords, "dev-swap", messages_for=load)
        try:
            assert loading.wait(5)
            shared._RUNNING_TURNS.pop(sid)
            lastwords.note_turn_end(sid, tid, outcome="completed")
        finally:
            resume.set()
        assert write.result(5) == 0
    assert not lastwords._path(sid).exists()


def test_atomic_writers_use_separate_temp_files(isolated_home, monkeypatch):
    destination = isolated_home / "atomic.json"
    replace = Path.replace
    ready = Barrier(2, timeout=5)
    sources = []

    def publish(source, target):
        if target == destination:
            sources.append(source)
            ready.wait()
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = [pool.submit(lastwords._write_atomic, destination, {"write": i}) for i in range(2)]
        for write in writes:
            write.result(5)
    assert len(set(sources)) == 2
    assert json.loads(destination.read_text())["write"] in (0, 1)
    assert not list(isolated_home.glob("*.tmp"))


@pytest.fixture
def recovery_fixture(isolated_home, monkeypatch):
    def seed(*, mode="notify", peer="", sid="recovery-fixture"):
        interrupts = []
        messages = []

        async def update(_config, value, **_):
            messages[:] = add_messages(messages, value["messages"])

        graph = SimpleNamespace(
            checkpointer=SimpleNamespace(get_pending_interrupt=lambda _: interrupts),
            aupdate_state=update,
        )
        session = {"session_id": sid, "project_slug": "default", "agent_id": "test-agent",
                   "graph": graph, "remote_peer": peer}
        monkeypatch.setattr(sessions, "_ensure_session", lambda key: session if key == sid else None)
        (isolated_home / "settings.json").write_text(json.dumps({"devswap": {"lastwords": {"mode": mode}}}))
        snap = {"session_id": sid, "project": "default", "turn_id": "interrupted",
                "reason": "signal", "ts": time.time(), "user_request_head": "Original request"}
        lastwords._write_atomic(lastwords._path(sid), snap)
        events = AsyncMock()
        monkeypatch.setattr(shared, "_push_global_event", events)
        return sid, snap, messages, interrupts, events

    return seed


async def test_same_pending_receipt_survives_restart(recovery_fixture):
    sid, _, messages, _, events = recovery_fixture()
    await lastwords.recover_lastwords()
    first = lastwords.list_pending()[0]
    lastwords._PENDING_RESUME.clear()
    await lastwords.recover_lastwords()
    assert lastwords.list_pending() == [first]
    assert len(messages) == 1
    assert messages[0].id == f"lastwords-{first['recovery_id']}"
    assert events.await_args_list[0].args == events.await_args_list[1].args
    assert lastwords._path(sid).exists()


async def test_auto_and_confirmation_check_busy_state_under_lock(recovery_fixture, monkeypatch):
    sid, _, _, _, _ = recovery_fixture(mode="auto")
    run = AsyncMock(return_value="completed")
    monkeypatch.setattr(stream, "_run_stream", run)
    lock = shared._turn_lock(sid)
    async with lock:
        assert await lastwords.recover_lastwords() == 0
        assert (await lastwords.resume_pending(sid))["error"] == "session_busy"
    run.assert_not_awaited()

    async def run_locked(*_args, **_kwargs):
        assert lock.locked()
        return "completed"

    run.side_effect = run_locked
    assert await lastwords.recover_lastwords() == 1
    run.assert_awaited_once()


async def test_confirmed_job_rechecks_parked_checkpoint_after_queueing(recovery_fixture, monkeypatch):
    sid, _, _, interrupts, events = recovery_fixture()
    run = AsyncMock()
    monkeypatch.setattr(stream, "_run_stream", run)
    await lastwords.recover_lastwords()
    pending = lastwords.list_pending()[0]
    spawned = []
    monkeypatch.setattr(shared, "spawn_bg", spawned.append)
    response = await lastwords.resume_pending(sid, pending["recovery_id"])
    assert response["status"] == "queued"
    assert response["updated_at"] > pending["updated_at"]
    interrupts.append(SimpleNamespace(value={"kind": "user_question", "qid": "q1"}))
    await spawned[0]
    run.assert_not_awaited()
    assert lastwords.list_pending() == []
    assert events.await_args.args[1]["status"] == "awaiting_input"
    assert lastwords._path(sid).exists()


async def test_failed_remote_recovery_keeps_original_after_wal_cleanup_and_restart(recovery_fixture, monkeypatch):
    sid, original, _, _, events = recovery_fixture(mode="auto", peer="test-peer")
    spawned = []
    monkeypatch.setattr(shared, "spawn_bg", spawned.append)
    calls = []

    async def fail(session, text, turn_id, **_):
        assert shared._turn_lock(sid).locked()
        calls.append(text)
        lastwords.note_remote_turn_start(sid, turn_id, session["project_slug"], text, peer="test-peer")
        lastwords.note_remote_turn_end(sid, turn_id)
        assert not lastwords._path(sid).exists()
        return {"ok": False, "state": "failed", "error": "synthetic failure"}

    monkeypatch.setattr(remote_turn, "run_remote_turn", fail)
    assert await lastwords.recover_lastwords() == 0
    item = lastwords.list_pending()[0]
    ack = await lastwords.resume_pending(sid, item["recovery_id"])
    assert ack["status"] == "queued"
    assert (await lastwords.resume_pending(sid, item["recovery_id"]))["error"] == "no_pending"
    await spawned[0]
    assert await lastwords.recover_lastwords() == 0
    assert calls == ["Original request"]
    assert all(call.args[0] != "runtime.lastwords_restored" for call in events.await_args_list)
    assert lastwords.list_receipts()[0]["status"] == "failed"
    receipt = json.loads(lastwords._receipt_path(sid).read_text())
    assert receipt["diagnostic"] == "remote_execution_failed"
    assert json.loads(lastwords._retained_path(receipt).read_text()) == original
    assert json.loads(lastwords._path(sid).read_text())["turn_id"] == original["turn_id"]


async def test_interrupted_attempt_reboots_to_confirmation(recovery_fixture, monkeypatch):
    sid, _, _, _, _ = recovery_fixture(mode="auto")
    started = asyncio.Event()

    async def interrupt_later(*_, **__):
        started.set()
        await asyncio.Future()

    run = AsyncMock(side_effect=interrupt_later)
    monkeypatch.setattr(stream, "_run_stream", run)
    recovery = asyncio.create_task(lastwords.recover_lastwords())
    await asyncio.wait_for(started.wait(), 3)
    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert await lastwords.recover_lastwords() == 0
    assert lastwords.list_pending()[0]["uncertain"] is True
    assert lastwords.list_pending()[0]["session_id"] == sid
    run.assert_awaited_once()


async def test_startup_recovers_independently_with_bounded_concurrency(recovery_fixture, monkeypatch):
    first, original, _, _, events = recovery_fixture(mode="auto", sid="recovery-a")
    second, third, remote = "recovery-b", "recovery-c", "recovery-remote"
    base = sessions._ensure_session(first)
    for sid in (second, third, remote):
        lastwords._write_atomic(lastwords._path(sid), {
            **original, "session_id": sid, "turn_id": f"original-{sid}",
        })
    monkeypatch.setattr(sessions, "_ensure_session", lambda sid: {
        **base, "session_id": sid, "remote_peer": "test-peer" if sid == remote else "",
    })
    second_started = asyncio.Event()
    third_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    invoked = []

    async def run(_ws, _graph, _config, _text, session, *_args, **_kwargs):
        sid = session["session_id"]
        assert shared._turn_lock(sid).locked()
        invoked.append(sid)
        if sid == first:
            await release_first.wait()
        elif sid == second:
            second_started.set()
            await release_second.wait()
            return "failed"
        elif sid == third:
            third_started.set()
        return "completed"

    monkeypatch.setattr(stream, "_run_stream", run)
    recovery = asyncio.create_task(lastwords.recover_lastwords())
    try:
        await asyncio.wait_for(second_started.wait(), 2)
        assert not recovery.done()
        assert [item["session_id"] for item in lastwords.list_pending()] == [remote]
        assert any(args.args[0] == "runtime.lastwords_pending" and args.args[1]["session_id"] == remote
                   for args in events.await_args_list)
        assert json.loads(lastwords._receipt_path(third).read_text())["status"] == "queued"
        assert invoked == [first, second]
        release_second.set()
        await asyncio.wait_for(third_started.wait(), 2)
        assert not release_first.is_set()
        assert json.loads(lastwords._receipt_path(second).read_text())["status"] == "failed"
        assert lastwords._path(second).exists()
    finally:
        release_first.set()
        release_second.set()
        restored = await asyncio.wait_for(recovery, 3)
    assert restored == 2
    assert invoked == [first, second, third]


async def test_queued_candidate_survives_dispatcher_shutdown(recovery_fixture, monkeypatch):
    first, original, _, _, _ = recovery_fixture(mode="auto", sid="recovery-a")
    second = "recovery-b"
    base = sessions._ensure_session(first)
    lastwords._write_atomic(lastwords._path(second), {
        **original, "session_id": second, "turn_id": "second-original",
    })
    monkeypatch.setattr(sessions, "_ensure_session", lambda sid: {**base, "session_id": sid})
    monkeypatch.setattr(lastwords, "STARTUP_RECOVERY_CONCURRENCY", 1)
    started = asyncio.Event()
    invoked = []

    async def run(_ws, _graph, _config, _text, session, *_args, **_kwargs):
        sid = session["session_id"]
        invoked.append(sid)
        if sid == first:
            started.set()
            await asyncio.Future()
        return "completed"

    monkeypatch.setattr(stream, "_run_stream", run)
    recovery = asyncio.create_task(lastwords.recover_lastwords())
    try:
        await asyncio.wait_for(started.wait(), 2)
        queued = json.loads(lastwords._receipt_path(second).read_text())
        assert queued["status"] == "queued"
        assert invoked == [first]
    finally:
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    assert lastwords._path(second).exists()
    assert await lastwords.recover_lastwords() == 1
    assert invoked == [first, second]
    assert lastwords.list_pending()[0]["session_id"] == first
    completed = json.loads(lastwords._receipt_path(second).read_text())
    assert (completed["recovery_id"], completed["status"]) == (queued["recovery_id"], "completed")


async def test_queued_auto_rechecks_remote_peer_before_dispatch(recovery_fixture, monkeypatch):
    first, original, _, _, _ = recovery_fixture(mode="auto", sid="recovery-a")
    second = "recovery-b"
    base = sessions._ensure_session(first)
    lastwords._write_atomic(lastwords._path(second), {
        **original, "session_id": second, "turn_id": "second-original",
    })
    late_peer = ""
    monkeypatch.setattr(sessions, "_ensure_session", lambda sid: {
        **base, "session_id": sid, "remote_peer": late_peer if sid == second else "",
    })
    monkeypatch.setattr(lastwords, "STARTUP_RECOVERY_CONCURRENCY", 1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "completed"

    local_run = AsyncMock(side_effect=run)
    remote_run = AsyncMock()
    monkeypatch.setattr(stream, "_run_stream", local_run)
    monkeypatch.setattr(remote_turn, "run_remote_turn", remote_run)
    recovery = asyncio.create_task(lastwords.recover_lastwords())
    try:
        await asyncio.wait_for(started.wait(), 2)
        late_peer = "new-remote"
    finally:
        release.set()
        restored = await asyncio.wait_for(recovery, 3)
    assert restored == 1
    local_run.assert_awaited_once()
    remote_run.assert_not_awaited()
    pending, = lastwords.list_pending()
    assert pending["session_id"] == second
    assert pending["remote"] is True


async def test_old_id_cannot_accept_or_dismiss_new_candidate(recovery_fixture):
    sid, snap, _, _, _ = recovery_fixture()
    await lastwords.recover_lastwords()
    old = lastwords.list_pending()[0]["recovery_id"]
    lastwords._PENDING_RESUME.clear()
    lastwords._write_atomic(lastwords._path(sid), {**snap, "turn_id": "another"})
    await lastwords.recover_lastwords()
    current = lastwords.list_pending()[0]["recovery_id"]
    assert current != old
    assert (await lastwords.resume_pending(sid, old))["error"] == "stale_recovery"
    assert lastwords.dismiss_pending(sid, old)["error"] == "stale_recovery"
    assert lastwords.dismiss_pending(sid, current)["had"] is True
    lastwords._write_atomic(lastwords._path(sid), {**snap, "turn_id": "another"})
    await lastwords.recover_lastwords()
    assert not lastwords.list_pending()
