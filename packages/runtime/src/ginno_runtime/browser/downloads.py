"""Browser downloads → ``~/.ginno/browser/downloads`` + Artifacts (design §13).

The engine records completed files here. Sidecar is the authority; Rust
never sees the download list.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import spaces as space_store

log = logging.getLogger(__name__)


def downloads_dir() -> Path:
    return space_store.browser_dir() / "downloads"


def index_path() -> Path:
    return downloads_dir() / "index.json"


def ensure_downloads_dir() -> Path:
    dest = downloads_dir()
    dest.mkdir(parents=True, exist_ok=True)
    if not index_path().exists():
        _atomic_write(index_path(), {"downloads": []})
    return dest


def _atomic_write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _read() -> list[dict[str, Any]]:
    p = index_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return []
    items = raw.get("downloads") if isinstance(raw, dict) else None
    return list(items) if isinstance(items, list) else []


def _write(items: list[dict[str, Any]]) -> None:
    ensure_downloads_dir()
    _atomic_write(index_path(), {"downloads": items[-80:]})


def list_downloads(space: str | None = None) -> list[dict[str, Any]]:
    items = [d for d in _read() if isinstance(d, dict)]
    if space:
        items = [d for d in items if d.get("space") == space]
    items.sort(key=lambda d: float(d.get("ts") or 0), reverse=True)
    return items


def record(
    entry: dict[str, Any],
    *,
    session_id: str | None = None,
    project_slug: str | None = None,
) -> dict[str, Any]:
    """Upsert one download by ``id`` (CDP guid) and optionally register an artifact."""
    ensure_downloads_dir()
    rec = {
        "id": str(entry.get("id") or entry.get("guid") or f"dl-{int(time.time() * 1000)}"),
        "space": entry.get("space") or "",
        "url": entry.get("url") or "",
        "filename": entry.get("filename") or "",
        "path": entry.get("path") or "",
        "state": entry.get("state") or "in_progress",
        "bytes": int(entry.get("bytes") or 0),
        "ts": float(entry.get("ts") or time.time()),
        "artifact": entry.get("artifact"),
    }
    sid = session_id or entry.get("session_id")
    slug = project_slug
    if rec["state"] == "completed" and rec["path"] and not rec.get("artifact"):
        rec["artifact"] = _try_artifact(rec, session_id=sid, project_slug=slug)
    items = _read()
    for i, it in enumerate(items):
        if isinstance(it, dict) and it.get("id") == rec["id"]:
            merged = {**it, **{k: v for k, v in rec.items() if v or k in ("bytes", "state")}}
            if rec.get("artifact"):
                merged["artifact"] = rec["artifact"]
            items[i] = merged
            _write(items)
            return merged
    items.append(rec)
    _write(items)
    return rec


def _try_artifact(
    rec: dict[str, Any],
    *,
    session_id: str | None,
    project_slug: str | None,
) -> dict[str, Any] | None:
    path = rec.get("path") or ""
    if not path:
        return None
    try:
        from ..artifacts import store as art_store

        slug = project_slug
        if not slug and session_id:
            from ..session_meta import _session_slug

            slug = _session_slug(session_id)
        slug = slug or "default"
        name = rec.get("filename") or Path(path).name
        return art_store.add_artifact(slug, "file", f"download {name}", path, session_id)
    except Exception:
        log.debug("download artifact register failed", exc_info=True)
        return None
