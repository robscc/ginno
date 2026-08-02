"""File registry — identity ledger for files a session cares about.

Every uploaded or agent-produced file becomes a ``FileEntry`` with a stable
``id`` ↔ canonical ``path`` mapping, persisted per project at
``projects/<slug>/files.json`` (no database, consistent with Ginno's
file-only storage). The registry is also the reactive touch-point:

- ``touch(path, reason)`` is called when any tool/MCP call mutates a file;
  it notifies per-path subscribers (the WS layer subscribes while a preview
  is open → ``preview.invalidate`` → UI refetch).
- ``list_session(session_id)`` bounds the mtime watcher's stat set to the
  session's artifacts instead of the whole workspace.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import paths

# A FileEntry is a plain dict (JSON-friendly):
#   {id, name, path, kind, mime, size, session_id, project_slug,
#    created, artifact_id, mtime, stale}
FileEntry = dict[str, Any]

TouchCallback = Callable[[FileEntry, str], None]

_BY_ID: dict[str, FileEntry] = {}
_BY_PATH: dict[str, FileEntry] = {}
_SUBSCRIBERS: dict[str, list[TouchCallback]] = {}
_LOADED = False


def _norm(p: str | Path) -> str:
    return str(Path(p).expanduser().resolve())


# Public alias: callers that store paths alongside registry entries (e.g.
# artifact refs) must normalize identically, or lookups by path mismatch on
# symlinked prefixes (macOS: /tmp → /private/tmp).
norm_path = _norm


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    root = paths.home() / "projects"
    if root.is_dir():
        for idx in root.glob("*/files.json"):
            try:
                import json

                for e in json.loads(idx.read_text() or "[]"):
                    _BY_ID[e["id"]] = e
                    _BY_PATH[_norm(e["path"])] = e
            except Exception:
                continue
    _LOADED = True


def _persist(slug: str) -> None:
    entries = [e for e in _BY_ID.values() if e.get("project_slug") == slug]
    p = paths.files_index_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    import json

    p.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


class FileRegistry:
    """Per-project file ledger. Use :func:`get_registry` for shared instances."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        _ensure_loaded()

    # ---- writes ----
    def register(
        self,
        name: str,
        path: str | Path,
        kind: str = "",
        mime: str = "",
        size: int = 0,
        session_id: str = "",
        artifact_id: str | None = None,
    ) -> FileEntry:
        """Register a file (idempotent by path: re-registering updates it)."""
        from .extractors import classify

        norm = _norm(path)
        existing = _BY_PATH.get(norm)
        try:
            st = Path(norm).stat()
            mtime = st.st_mtime
            size = size or st.st_size
        except OSError:
            mtime = 0.0
        if existing is not None:
            existing.update(
                {
                    "name": name,
                    "kind": kind or classify(norm),
                    "mime": mime or existing.get("mime", ""),
                    "size": size,
                    "session_id": session_id or existing.get("session_id", ""),
                    "artifact_id": artifact_id or existing.get("artifact_id"),
                    "mtime": mtime,
                    "stale": False,
                }
            )
            _persist(self.slug)
            return existing
        entry: FileEntry = {
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "path": norm,
            "kind": kind or classify(norm),
            "mime": mime,
            "size": size,
            "session_id": session_id,
            "project_slug": self.slug,
            "created": time.time(),
            "artifact_id": artifact_id,
            "mtime": mtime,
            "stale": False,
        }
        _BY_ID[entry["id"]] = entry
        _BY_PATH[norm] = entry
        _persist(self.slug)
        return entry

    def mark_stale(self, file_id: str, stale: bool = True) -> FileEntry | None:
        e = _BY_ID.get(file_id)
        if e is not None:
            e["stale"] = stale
        return e

    def set_kind(self, path: str | Path, kind: str) -> FileEntry | None:
        """Override the classified kind for a path (user correction) + persist."""
        e = _BY_PATH.get(_norm(path))
        if e is not None and kind:
            e["kind"] = kind
            _persist(self.slug)
        return e

    # ---- reads ----
    def get(self, file_id: str) -> FileEntry | None:
        return _BY_ID.get(file_id)

    def find_by_path(self, path: str | Path) -> FileEntry | None:
        return _BY_PATH.get(_norm(path))

    def list_all(self) -> list[FileEntry]:
        return [e for e in _BY_ID.values() if e.get("project_slug") == self.slug]

    def list_session(self, session_id: str) -> list[FileEntry]:
        return [
            e
            for e in _BY_ID.values()
            if e.get("project_slug") == self.slug and e.get("session_id") == session_id
        ]


_REGISTRIES: dict[str, FileRegistry] = {}


def get_registry(slug: str) -> FileRegistry:
    reg = _REGISTRIES.get(slug)
    if reg is None:
        reg = FileRegistry(slug)
        _REGISTRIES[slug] = reg
    return reg


def get_by_id(file_id: str) -> FileEntry | None:
    """Cross-project lookup by id (REST endpoints don't know the slug)."""
    _ensure_loaded()
    return _BY_ID.get(file_id)


# --------------------------------------------------------------------------
# reactive layer: touch + subscribe
# --------------------------------------------------------------------------

def touch(path: str | Path, reason: str = "write") -> list[FileEntry]:
    """A file changed (tool/MCP wrote it). Notify subscribers for its path.

    Returns the registered entries matched (may be empty for untracked files).
    """
    _ensure_loaded()
    norm = _norm(path)
    entries = [e for e in _BY_PATH.values() if _norm(e["path"]) == norm]
    for e in entries:
        try:
            e["mtime"] = Path(norm).stat().st_mtime
        except OSError:
            pass
    for cb in list(_SUBSCRIBERS.get(norm, [])):
        for e in entries:
            try:
                cb(e, reason)
            except Exception:
                continue
    return entries


def subscribe(path: str | Path, cb: TouchCallback) -> Callable[[], None]:
    """Register a touch callback for a path; returns an unsubscribe fn."""
    norm = _norm(path)
    _SUBSCRIBERS.setdefault(norm, []).append(cb)

    def unsub() -> None:
        lst = _SUBSCRIBERS.get(norm, [])
        if cb in lst:
            lst.remove(cb)

    return unsub


def reset_registries() -> None:
    """Clear all caches (tests redirect GINNO_HOME between cases)."""
    global _LOADED
    _BY_ID.clear()
    _BY_PATH.clear()
    _SUBSCRIBERS.clear()
    _REGISTRIES.clear()
    _LOADED = False
