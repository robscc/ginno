"""Regression tests for the TODO sync ledger's on-disk transactions."""

import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.todos import sync_ledger


def _append(run_id):
    return sync_ledger.append("待办", "provider", "external", "push", run_id)


@pytest.mark.parametrize("failure", [OSError("replace failed"), KeyboardInterrupt()])
def test_replace_failure_preserves_ledger(isolated_home, monkeypatch, failure):
    path = isolated_home / "todo_sync.json"
    original = _append("original")
    old_bytes = path.read_bytes()
    replacements = []
    replace = os.replace

    def interrupt_replace(src, dst):
        if dst != path:
            return replace(src, dst)
        assert src.parent == path.parent
        assert json.loads(src.read_text()) == [{**original, "status": "ok"}]
        assert dst.read_bytes() == old_bytes
        replacements.append(src)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_replace)
        with pytest.raises(type(failure)) as raised:
            sync_ledger.set_status("original", "ok")
        assert raised.value is failure

    assert path.read_bytes() == old_bytes
    assert sync_ledger.latest() == [original]
    assert len(replacements) == 1
    assert not replacements[0].exists()
    sync_ledger.set_status("original", "ok")
    assert sync_ledger.latest() == [{**original, "status": "ok"}]


def test_concurrent_append_update_and_retry(isolated_home):
    path = isolated_home / "todo_sync.json"
    _append("original")
    path.chmod(0o640)
    worker_count = 16
    start = threading.Barrier(worker_count)

    def update(i):
        start.wait(timeout=10)
        run_id = f"run-{i}"
        _append(run_id)
        sync_ledger.set_status(run_id, "failed", f"error-{i}")
        assert sync_ledger.clone_for_retry(run_id, f"retry-{i}")

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(update, range(worker_count)))

    rows = json.loads(path.read_text())
    assert rows == sync_ledger.latest()
    by_run = {row["run_id"]: row for row in rows}
    assert len(rows) == len(by_run) == 1 + 2 * worker_count
    for i in range(worker_count):
        row = by_run[f"run-{i}"]
        assert (row["status"], row["error"]) == ("failed", f"error-{i}")
        retry = by_run[f"retry-{i}"]
        assert (retry["status"], retry["error"], retry["todo_id"]) == ("running", "", "待办")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert "待办" in path.read_text()
    assert not list(isolated_home.glob("*.tmp"))


@pytest.mark.parametrize("malformed", [b'[{"run_id": "old"', b'{"run_id": "old"}', b""])
def test_append_refuses_to_replace_corrupt_ledger(isolated_home, malformed):
    path = isolated_home / "todo_sync.json"
    path.write_bytes(malformed)

    assert sync_ledger.latest() == []
    assert sync_ledger.set_status("untracked", "ok") is None
    assert sync_ledger.clone_for_retry("untracked", "retry") is False
    with pytest.raises(ValueError):
        _append("new")
    assert path.read_bytes() == malformed


def test_concurrent_processes_append_and_update(isolated_home):
    pytest.importorskip("fcntl")
    worker = """
import sys
from ginno_runtime.todos import sync_ledger

for i in range(12):
    run_id = f"{sys.argv[1]}-{i}"
    sync_ledger.append("todo", "provider", "external", "push", run_id)
    sync_ledger.set_status(run_id, "ok")
"""
    worker_count = 4
    start = threading.Barrier(worker_count)

    def run(i):
        start.wait(timeout=10)
        result = subprocess.run(
            [sys.executable, "-c", worker, str(i)], capture_output=True, text=True, timeout=20
        )
        assert result.returncode == 0, result.stderr

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(run, range(worker_count)))

    rows = json.loads((isolated_home / "todo_sync.json").read_text())
    expected = {f"{i}-{j}": "ok" for i in range(worker_count) for j in range(12)}
    assert len(rows) == len(expected)
    assert {row["run_id"]: row["status"] for row in rows} == expected
