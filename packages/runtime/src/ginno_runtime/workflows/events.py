"""Append-only execution event log per run (design doc §7).

Each run writes JSONL lines to ``~/.ginno/workflow_runs/<run_id>.events.jsonl``.
P1 provides append/read only; the SSE/WS live stream is wired in P2 when the
executor starts emitting events. Lines are append-only and never rewritten, so
concurrent appends from a single run are safe enough for v1 (one writer per run).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import paths


def _events_path(run_id: str) -> Path:
    return paths.home() / "workflow_runs" / f"{run_id}.events.jsonl"


def append_event(run_id: str, kind: str, **data: Any) -> dict[str, Any]:
    """Append one event line and return the event dict that was written."""
    ev: dict[str, Any] = {
        "ts": time.time(),
        "run_id": run_id,
        "kind": kind,
        **{k: v for k, v in data.items() if v is not None},
    }
    p = _events_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def read_events(
    run_id: str,
    node_id: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Read events for a run, optionally filtered by node_id and/or kind."""
    p = _events_path(run_id)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if node_id is not None and ev.get("node_id") != node_id:
            continue
        if kind is not None and ev.get("kind") != kind:
            continue
        out.append(ev)
    return out


def delete_events(run_id: str) -> bool:
    """Remove a run's events JSONL. Returns True if the file existed."""
    p = _events_path(run_id)
    existed = p.exists()
    p.unlink(missing_ok=True)
    return existed
