"""Append-only ledger of TODO <-> platform sync runs (~/.ginno/todo_sync.json).

Ext refs on a todo are the *relation*; this ledger records the *events*
(who/when/which run/what result) so the panel can show per-provider sync
status and offer retry without polluting the todo schema.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import paths

_MAX_ENTRIES = 200


def _path():
    return paths.home() / "todo_sync.json"


def _read() -> list[dict[str, Any]]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _write(items: list[dict[str, Any]]) -> None:
    _path().write_text(json.dumps(items[-_MAX_ENTRIES:], indent=2, ensure_ascii=False))


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
    items = _read()
    items.append(entry)
    _write(items)
    return entry


def set_status(run_id: str, status: str, error: str = "") -> None:
    """No-op unless the run is a tracked todo-sync run."""
    items = _read()
    for e in reversed(items):
        if e.get("run_id") == run_id:
            e["status"] = status
            if error:
                e["error"] = error
            break
    else:
        return
    _write(items)


def clone_for_retry(old_run_id: str, new_run_id: str) -> bool:
    """Carry the sync-relation of a failed/interrupted run over to its retry.

    Finds the latest ledger entry for ``old_run_id`` and re-appends the same
    todo_id/provider/ext_id/direction under ``new_run_id`` (status=running), so
    the TodoPanel keeps showing per-provider sync state across a retry. No-op
    (False) for runs that were never todo-sync runs.
    """
    items = _read()
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
    _write(items)
    return True


def latest(limit: int = 100) -> list[dict[str, Any]]:
    return _read()[-limit:]
