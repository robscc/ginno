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

# Per-run monotonic event counters (design B P2): every appended event gets a
# ``seq`` so the run-scoped WS client can drop duplicates after a reconnect
# (snapshot + live frames may overlap). Seeded lazily from the file's line
# count so a sidecar restart continues the sequence instead of restarting at 1.
_SEQ: dict[str, int] = {}


def _events_path(run_id: str) -> Path:
    return paths.home() / "workflow_runs" / f"{run_id}.events.jsonl"


def _next_seq(run_id: str) -> int:
    if run_id not in _SEQ:
        p = _events_path(run_id)
        n = 0
        if p.exists():
            n = sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
        _SEQ[run_id] = n
    _SEQ[run_id] += 1
    return _SEQ[run_id]


def append_event(run_id: str, kind: str, **data: Any) -> dict[str, Any]:
    """Append one event line and return the event dict that was written."""
    ev: dict[str, Any] = {
        "ts": time.time(),
        "run_id": run_id,
        "kind": kind,
        "seq": _next_seq(run_id),
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
    """Read events for a run, optionally filtered by node_id and/or kind.

    Legacy lines written before ``seq`` existed get their line index stamped in
    (strictly below any live seq) so a WS client always sees a total order."""
    p = _events_path(run_id)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "seq" not in ev:
            ev["seq"] = i + 1
        if node_id is not None and ev.get("node_id") != node_id:
            continue
        if kind is not None and ev.get("kind") != kind:
            continue
        out.append(ev)
    return out


def delete_events(run_id: str) -> bool:
    """Remove a run's events JSONL (and its seq counter). True if it existed."""
    p = _events_path(run_id)
    existed = p.exists()
    p.unlink(missing_ok=True)
    _SEQ.pop(run_id, None)
    return existed
