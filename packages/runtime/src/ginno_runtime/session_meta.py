"""Session metadata (per-project ``sessions/_index.json``) access helpers.

Shared by the sessions, usage, files and streaming modules — kept out of
server.py so api/ router modules can use them without importing the app
module (which would create an import cycle).
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import paths
from .server_shared import _SESSIONS

# 2026-10-05 事故:20 路并发委托各自「读-改-写」同一 _index.json,最后写的
# 抱着过期快照把其余条目全部踩掉(索引只剩 1 条、侧栏清空)。索引所有写
# 路径必须串行(进程内 threading 锁足够——调用方横跨事件循环与委托 bg
# 线程)+ 原子落盘(tmp+rename),读端永远只见完整文件。
_IDX_LOCK = threading.RLock()


def _write_index(slug: str, items: list[dict]) -> None:
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    p = paths.session_index_path(slug)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _session_meta_rewrite_all(slug: str, metas: list[dict]) -> None:
    """整表替换(heal/迁移路径专用)——同样走锁,免得绕开串行约束。"""
    with _IDX_LOCK:
        _write_index(slug, metas)


def _session_meta_list(slug: str) -> list[dict]:
    p = paths.session_index_path(slug)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def _session_meta_upsert(slug: str, entry: dict) -> None:
    with _IDX_LOCK:
        items = [m for m in _session_meta_list(slug) if m.get("id") != entry["id"]]
        items.insert(0, entry)
        _write_index(slug, items)


def _session_meta_patch(slug: str, session_id: str, patch: dict) -> dict | None:
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        target = None
        for m in items:
            if m.get("id") == session_id:
                m.update({k: v for k, v in patch.items() if v is not None})
                m["updated"] = time.time()
                target = m
        if target is None:
            return None
        _write_index(slug, items)
        return target


def _session_meta_remove(slug: str, session_id: str) -> bool:
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        kept = [m for m in items if m.get("id") != session_id]
        if len(kept) == len(items):
            return False
        _write_index(slug, kept)
        return True


def _session_meta_children(slug: str, session_id: str) -> list[dict]:
    """Direct subagent children (``parent_session_id == session_id``) from the
    on-disk index — the source of truth for the parent links (subagent-design.md
    §4); the in-memory ``_SESSION_CHILDREN`` mirror is only a fast path."""
    return [
        m for m in _session_meta_list(slug) if m.get("parent_session_id") == session_id
    ]


def _session_meta_descendants(slug: str, session_id: str) -> list[dict]:
    """All subagent descendants, depth-first (parent/child links form a tree —
    the depth cap guarantees acyclicity)."""
    out: list[dict] = []

    def _walk(pid: str) -> None:
        for m in _session_meta_children(slug, pid):
            out.append(m)
            _walk(m["id"])

    _walk(session_id)
    return out


def subagent_depth_of(meta: dict | None) -> int:
    """Subagent depth encoded in a session meta: 0/1/2 for subagent sessions,
    -1 for a main conversation (no subagent fields)."""
    if not meta or meta.get("type") != "subagent":
        return -1
    try:
        return int(meta.get("depth"))
    except (TypeError, ValueError):
        return -1


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
