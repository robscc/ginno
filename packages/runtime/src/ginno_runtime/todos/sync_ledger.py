"""Append-only ledger of TODO <-> platform sync runs (~/.ginno/todo_sync.json).

Ext refs on a todo are the *relation*; this ledger records the *events*
(who/when/which run/what result) so the panel can show per-provider sync
status and offer retry without polluting the todo schema.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ginno_runtime import jsonx as json

from .. import paths
from ..atomic_json import write_json

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_MAX_ENTRIES = 200
_LOCK = threading.Lock()


def _path() -> Path:
    return paths.home() / "todo_sync.json"


@contextmanager
def _mutation(p: Path):
    # The side lock stays on the same inode while the ledger is replaced.
    with _LOCK:
        if fcntl is None:
            yield
        else:
            with p.with_name(p.name + ".lock").open("a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield


def _read(p: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    try:
        text = p.read_text()
    except FileNotFoundError:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if strict:
            raise
        return []
    if isinstance(data, list) and all(isinstance(entry, dict) for entry in data):
        return data
    if strict:
        raise ValueError(f"Invalid sync ledger entries in {p}")
    return []


def _write(p: Path, items: list[dict[str, Any]]) -> None:
    write_json(p, items[-_MAX_ENTRIES:], preserve_mode=True)


def append(
    todo_id: str,
    provider: str,
    ext_id: str,
    direction: str,
    run_id: str,
    status: str = "running",
    error: str = "",
) -> dict[str, Any]:
    entry = {
        "todo_id": todo_id,
        "provider": provider,
        "ext_id": ext_id,
        "direction": direction,
        "run_id": run_id,
        "status": status,
        "error": error,
        "at": time.time(),
    }
    p = _path().resolve()
    with _mutation(p):
        items = _read(p, strict=True)
        items.append(entry)
        _write(p, items)
    return entry


def set_status(run_id: str, status: str, error: str = "") -> None:
    """No-op unless the run is a tracked todo-sync run."""
    p = _path().resolve()
    if not p.exists():
        return
    with _mutation(p):
        items = _read(p)
        for e in reversed(items):
            if e.get("run_id") == run_id:
                e["status"] = status
                if error:
                    e["error"] = error
                _write(p, items)
                return


def clone_for_retry(old_run_id: str, new_run_id: str) -> bool:
    """Carry the sync-relation of a failed/interrupted run over to its retry.

    Finds the latest ledger entry for ``old_run_id`` and re-appends the same
    todo_id/provider/ext_id/direction under ``new_run_id`` (status=running), so
    the TodoPanel keeps showing per-provider sync state across a retry. No-op
    (False) for runs that were never todo-sync runs.
    """
    p = _path().resolve()
    if not p.exists():
        return False
    with _mutation(p):
        items = _read(p)
        src = next((e for e in reversed(items) if e.get("run_id") == old_run_id), None)
        if src is None:
            return False
        items.append(
            {
                "todo_id": src.get("todo_id", ""),
                "provider": src.get("provider", ""),
                "ext_id": src.get("ext_id", ""),
                "direction": src.get("direction", ""),
                "run_id": new_run_id,
                "status": "running",
                "error": "",
                "at": time.time(),
            }
        )
        _write(p, items)
        return True


def latest(limit: int = 100) -> list[dict[str, Any]]:
    return _read(_path())[-limit:]
