"""Wiki citation usage ledger (docs/citations-design.md §3.2 / §6.1).

``~/.ginno/knowledge/usage.json`` — per-page counters, atomic writes (temp +
os.replace, the todos/checkpointer pattern), an in-process lock (the sidecar
is a single process), and checksum binding so content drift can be detected
(a page edited after its last use is marked ``drifted`` by readers).

Only counters and ids are stored — never answer content or notes.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .. import paths

_LOCK = threading.RLock()

INVALID_KEY = "_invalid"
_INVALID_SAMPLE_CAP = 20


def usage_path() -> Path:
    return paths.knowledge_dir() / "usage.json"


def _load() -> dict[str, Any]:
    p = usage_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    p = usage_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _entry(data: dict[str, Any], rel_path: str) -> dict[str, Any]:
    e = data.get(rel_path)
    if not isinstance(e, dict):
        e = {}
        data[rel_path] = e
    return e


def record_injected(rel_paths: list[str], checksums: dict[str, str] | None = None) -> None:
    """Count one injection per page (deduped per call = per turn)."""
    checksums = checksums or {}
    if not rel_paths:
        return
    now = time.time()
    with _LOCK:
        data = _load()
        for rel in dict.fromkeys(rel_paths):  # dedupe, keep order
            if not rel:
                continue
            e = _entry(data, rel)
            e["injected"] = int(e.get("injected") or 0) + 1
            e["last_injected"] = now
            ck = checksums.get(rel)
            if ck:
                e["checksum"] = ck
        _save(data)


def record_cited(
    rel_path: str,
    session_id: str = "",
    turn_id: str = "",
    index_only: bool = False,
) -> None:
    """Count a verified citation (or an index_only hit — retrieval-blind spot)."""
    if not rel_path:
        return
    now = time.time()
    with _LOCK:
        data = _load()
        e = _entry(data, rel_path)
        if index_only:
            e["cited_index_only"] = int(e.get("cited_index_only") or 0) + 1
        else:
            e["cited"] = int(e.get("cited") or 0) + 1
            e["last_cited"] = now
            if session_id:
                e["last_session"] = session_id
            if turn_id:
                e["last_turn"] = turn_id
        _save(data)


def record_invalid(samples: list[str]) -> None:
    """Count unverified (hallucinated) wiki refs; keep a small sample ring."""
    samples = [s for s in (samples or []) if s]
    if not samples:
        return
    with _LOCK:
        data = _load()
        e = _entry(data, INVALID_KEY)
        e["cited"] = int(e.get("cited") or 0) + len(samples)
        ring = list(e.get("samples") or [])
        ring.extend(samples)
        e["samples"] = ring[-_INVALID_SAMPLE_CAP:]
        _save(data)


def is_drifted(entry: dict[str, Any], current_checksum: str | None) -> bool:
    """True when the page content changed since its last usage accounting."""
    stored = entry.get("checksum")
    if not stored or not current_checksum:
        return False
    return stored != current_checksum


def all_usage() -> dict[str, Any]:
    with _LOCK:
        return _load()


def top(sort: str = "cited", limit: int = 20) -> list[dict[str, Any]]:
    """Ranked page rows for the API/UI. ``sort`` ∈ cited|injected|rate."""
    data = all_usage()
    rows: list[dict[str, Any]] = []
    for rel, e in data.items():
        if rel == INVALID_KEY or not isinstance(e, dict):
            continue
        injected = int(e.get("injected") or 0)
        cited = int(e.get("cited") or 0)
        rows.append(
            {
                "path": rel,
                "injected": injected,
                "cited": cited,
                "cited_index_only": int(e.get("cited_index_only") or 0),
                "rate": round(cited / injected, 3) if injected else 0.0,
                "last_injected": e.get("last_injected"),
                "last_cited": e.get("last_cited"),
                "last_session": e.get("last_session"),
            }
        )
    key = {
        "cited": lambda r: (-r["cited"], -r["injected"], r["path"]),
        "injected": lambda r: (-r["injected"], -r["cited"], r["path"]),
        "rate": lambda r: (-r["rate"], -r["cited"], r["path"]),
    }.get(sort, None)
    rows.sort(key=key or (lambda r: r["path"]))
    return rows[: max(1, min(int(limit or 20), 200))]


def summary() -> dict[str, Any]:
    """Aggregate counters for the KB stats endpoint."""
    data = all_usage()
    total_injected = total_cited = total_index_only = pages = 0
    for rel, e in data.items():
        if rel == INVALID_KEY or not isinstance(e, dict):
            continue
        pages += 1
        total_injected += int(e.get("injected") or 0)
        total_cited += int(e.get("cited") or 0)
        total_index_only += int(e.get("cited_index_only") or 0)
    inv = data.get(INVALID_KEY) or {}
    return {
        "pages_tracked": pages,
        "total_injected": total_injected,
        "total_cited": total_cited,
        "total_cited_index_only": total_index_only,
        "citation_rate": round(total_cited / total_injected, 3) if total_injected else 0.0,
        "invalid_cited": int(inv.get("cited") or 0),
        "top_cited": top("cited", 5),
    }


def reset() -> None:
    with _LOCK:
        _save({})
