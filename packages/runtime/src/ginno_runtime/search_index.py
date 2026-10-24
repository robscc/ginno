"""Message-level search index (V2 P0-1 / P0-2).

Titles alone were not enough: the question users actually ask is "where did I
discuss X". This module keeps a small SQLite mirror of every session's message
text under ``~/.ginno/index/search.db`` so ``/api/search`` can answer without
parsing 477 checkpoint files per keystroke.

Design choices:

* plain ``LIKE`` scan instead of FTS5 — at this corpus size (hundreds of
  sessions) it is single-digit milliseconds, and it matches CJK substrings,
  which the default FTS5 tokenizers do not.
* incremental: a session is re-indexed only when its meta ``updated`` moved.
* best-effort: an unreadable checkpoint is skipped, never fatal.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

from . import paths
from .session_meta import _session_meta_list

_log = logging.getLogger("ginno.search")

_LOCK = threading.RLock()
_STATE = {"building": False, "last_build": 0.0, "indexed": 0}
_MAX_TEXT = 4000  # per message; tool output bodies are mostly noise
_SNIPPET_RADIUS = 60


def _db_path():
    d = paths.home() / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d / "search.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    # The indexer runs in a background thread while requests may search.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS msgs(
            session_id TEXT NOT NULL,
            message_id TEXT,
            seq INTEGER,
            role TEXT,
            text TEXT,
            session_title TEXT,
            session_updated REAL,
            archived INTEGER DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgs_session ON msgs(session_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS indexed("
        "session_id TEXT PRIMARY KEY, updated REAL, project_slug TEXT)"
    )
    return conn


def _project_slugs() -> list[str]:
    root = paths.home() / "projects"
    try:
        return sorted(
            p.name for p in root.iterdir() if (p / "sessions" / "_index.json").exists()
        )
    except OSError:
        return []


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text") or b.get("thinking") or ""
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


_INJECTED_PREFIXES = (
    "[turn context]",
    "[conversation summary]",
    "[world state re-injection]",
)


def _is_injected(text: str) -> bool:
    """Injected context rows are boilerplate, not something the user said."""
    return bool(text) and text.startswith(_INJECTED_PREFIXES)


def _load_messages(slug: str, session_id: str) -> list:
    from .checkpointer import FileCheckpointer

    tup = FileCheckpointer(slug).get_tuple(
        {"configurable": {"thread_id": session_id}}
    )
    if not tup or not tup.checkpoint:
        return []
    return (tup.checkpoint.get("channel_values") or {}).get("messages") or []


def build_index(force: bool = False) -> dict:
    """Incrementally index every session. Safe to call repeatedly."""
    with _LOCK:
        if _STATE["building"] and not force:
            return {"indexing": True, "indexed": _STATE["indexed"]}
        _STATE["building"] = True
    indexed_count = 0
    try:
        conn = _connect()
        try:
            known = {
                row[0]: (row[1] or 0.0)
                for row in conn.execute("SELECT session_id, updated FROM indexed")
            }
            live: set[str] = set()
            for slug in _project_slugs():
                for meta in _session_meta_list(slug):
                    sid = str(meta.get("id") or "")
                    if not sid:
                        continue
                    live.add(sid)
                    updated = float(meta.get("updated") or 0)
                    if not force and known.get(sid, -1) >= updated:
                        continue
                    try:
                        messages = _load_messages(slug, sid)
                    except Exception:  # noqa: BLE001 — one bad file, keep going
                        _log.warning("search_index_skip session=%s", sid, exc_info=True)
                        continue
                    title = str(meta.get("title") or "")
                    archived = 1 if meta.get("archived") else 0
                    rows = []
                    for seq, m in enumerate(messages):
                        text = _message_text(getattr(m, "content", "")).strip()
                        if not text:
                            continue
                        if str(getattr(m, "type", "")) == "human" and _is_injected(text):
                            continue
                        rows.append(
                            (
                                sid,
                                str(getattr(m, "id", "") or ""),
                                seq,
                                str(getattr(m, "type", "") or ""),
                                text[:_MAX_TEXT],
                                title,
                                updated,
                                archived,
                            )
                        )
                    conn.execute("DELETE FROM msgs WHERE session_id=?", (sid,))
                    conn.executemany(
                        "INSERT INTO msgs(session_id,message_id,seq,role,text,"
                        "session_title,session_updated,archived) VALUES(?,?,?,?,?,?,?,?)",
                        rows,
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO indexed(session_id,updated,project_slug)"
                        " VALUES(?,?,?)",
                        (sid, updated, slug),
                    )
                    indexed_count += 1
            # Drop sessions that no longer exist (deleted / purged).
            stale = [sid for sid in known if sid not in live]
            for sid in stale:
                conn.execute("DELETE FROM msgs WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM indexed WHERE session_id=?", (sid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the index is an optimisation, never fatal
        _log.exception("search_index_failed")
    finally:
        # Report the DB total, not this run's delta: an incremental pass with
        # nothing new still has an index worth telling the UI about.
        total = indexed_count
        try:
            conn = _connect()
            try:
                total = int(conn.execute("SELECT COUNT(*) FROM indexed").fetchone()[0])
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        with _LOCK:
            _STATE["building"] = False
            _STATE["last_build"] = time.time()
            _STATE["indexed"] = total
    if indexed_count:
        _log.info("search_index_built sessions=%d", indexed_count)
    return {"indexing": False, "indexed": indexed_count}


def status() -> dict:
    with _LOCK:
        return {
            "indexing": bool(_STATE["building"]),
            "last_build": _STATE["last_build"] or None,
            "indexed": int(_STATE["indexed"]),
        }


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(text: str, q: str) -> str:
    idx = text.lower().find(q.lower())
    if idx < 0:
        return text[:_SNIPPET_RADIUS * 2]
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(text), idx + len(q) + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def search_messages(q: str, limit: int = 20, archived: bool | None = None) -> list[dict]:
    needle = (q or "").strip()
    if not needle:
        return []
    try:
        conn = _connect()
    except sqlite3.Error:
        return []
    try:
        sql = (
            "SELECT session_id,message_id,role,text,session_title,archived "
            "FROM msgs WHERE text LIKE ? ESCAPE '\\'"
        )
        params: list = [_like(needle)]
        if archived is not None:
            sql += " AND archived=?"
            params.append(1 if archived else 0)
        sql += " ORDER BY session_updated DESC, seq DESC LIMIT ?"
        params.append(max(1, min(int(limit or 20), 100)))
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        _log.warning("search_query_failed", exc_info=True)
        return []
    finally:
        conn.close()
    out = []
    for sid, mid, role, text, title, arch in rows:
        out.append(
            {
                "session_id": sid,
                "message_id": mid,
                "role": role,
                "snippet": _snippet(str(text), needle),
                "session_title": title or "",
                "archived": bool(arch),
            }
        )
    return out


def index_size_bytes() -> int:
    try:
        return os.path.getsize(_db_path())
    except OSError:
        return 0

