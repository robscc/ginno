"""Session metadata (per-project ``sessions/_index.json``) access helpers.

Shared by the sessions, usage, files and streaming modules — kept out of
server.py so api/ router modules can use them without importing the app
module (which would create an import cycle).
"""

from __future__ import annotations

import json
import time

from . import paths
from .server_shared import _SESSIONS


def _session_meta_list(slug: str) -> list[dict]:
    p = paths.session_index_path(slug)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def _session_meta_upsert(slug: str, entry: dict) -> None:
    items = [m for m in _session_meta_list(slug) if m.get("id") != entry["id"]]
    items.insert(0, entry)
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    paths.session_index_path(slug).write_text(
        json.dumps(items, indent=2, ensure_ascii=False)
    )


def _session_meta_patch(slug: str, session_id: str, patch: dict) -> dict | None:
    items = _session_meta_list(slug)
    target = None
    for m in items:
        if m.get("id") == session_id:
            m.update({k: v for k, v in patch.items() if v is not None})
            m["updated"] = time.time()
            target = m
    if target is None:
        return None
    paths.session_index_path(slug).write_text(
        json.dumps(items, indent=2, ensure_ascii=False)
    )
    return target


def _session_meta_remove(slug: str, session_id: str) -> bool:
    items = _session_meta_list(slug)
    kept = [m for m in items if m.get("id") != session_id]
    if len(kept) == len(items):
        return False
    paths.session_index_path(slug).write_text(
        json.dumps(kept, indent=2, ensure_ascii=False)
    )
    return True


def _find_meta(session_id: str) -> tuple[dict, str] | None:
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m, slug
    return None


def _resolve_session_meta(session_id: str) -> dict | None:
    """Find a session's meta (with project_slug/workspace) in memory or on disk."""
    s = _SESSIONS.get(session_id)
    if s:
        return s
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m
    return None


def _session_slug(session_id: str) -> str | None:
    s = _SESSIONS.get(session_id)
    if s:
        return s["project_slug"]
    found = _find_meta(session_id)
    return found[1] if found else None
