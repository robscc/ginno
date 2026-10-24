"""Unified search endpoint (V2 P0-1 / P0-2).

Two result groups from one query:
* sessions — title / id match over the project index;
* messages — content match from ``search_index`` (SQLite mirror).

``scope=archived`` restricts both to archived sessions, which is what the
archive browser needs.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import search_index
from ..session_meta import _session_meta_list

router = APIRouter()


@router.get("/api/search")
async def search(
    q: str = Query(""),
    scope: str = Query("all"),
    limit: int = Query(20),
    project_slug: str | None = Query(None),
) -> dict:
    needle = (q or "").strip()
    slug = project_slug or "default"
    limit = max(1, min(int(limit or 20), 100))
    sc = scope if scope in ("all", "sessions", "messages", "archived") else "all"
    archived_only = sc == "archived"
    want_sessions = sc in ("all", "sessions", "archived")
    want_messages = sc in ("all", "messages", "archived")

    sessions: list[dict] = []
    if needle and want_sessions:
        low = needle.lower()
        for m in _session_meta_list(slug):
            if archived_only and not m.get("archived"):
                continue
            title = str(m.get("title") or "")
            sid = str(m.get("id") or "")
            if low in title.lower() or low in sid.lower():
                sessions.append({**m, "match": "title"})
        sessions.sort(key=lambda r: r.get("updated") or 0, reverse=True)
        sessions = sessions[:limit]

    messages: list[dict] = []
    if needle and want_messages:
        messages = search_index.search_messages(
            needle, limit=limit, archived=True if archived_only else None
        )

    return {
        "ok": True,
        "q": needle,
        "scope": sc,
        "index": search_index.status(),
        "sessions": sessions,
        "messages": messages,
    }
