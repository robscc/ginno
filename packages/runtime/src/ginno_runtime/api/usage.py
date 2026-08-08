"""Usage telemetry endpoints (usage-stats-design.md §5)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import usage_store
from ..server_shared import _SESSIONS, _USAGE_BY_SESSION
from ..session_meta import _find_meta
from ..usage import cache_hit_ratio

router = APIRouter()


@router.get("/api/sessions/{session_id}/usage")
async def get_session_usage(session_id: str) -> dict:
    """Per-session cumulative model usage (the TopBar counter). The live
    `usage` WS event only fires on turns; this lets the UI show a session's
    accumulated stats immediately after a session switch.

    Source order (usage-stats-design.md §5): the persistent usage log first —
    it survives runtime restarts, so a session's total stays truthful across
    restarts — falling back to the in-memory accumulator when nothing was
    logged yet. Response shape is unchanged."""
    logged = usage_store.session_totals(session_id)
    if logged:
        return {"ok": True, "usage": logged}
    acc = _USAGE_BY_SESSION.get(session_id)
    if not acc:
        return {"ok": True, "usage": None}
    return {"ok": True, "usage": {**acc, "cache_hit_ratio": cache_hit_ratio(acc)}}


def _usage_session_display(sid: str) -> dict:
    """Join display meta for a usage row. Deleted sessions keep their usage
    (billing-style data) under a placeholder title (design §3.3)."""
    s = _SESSIONS.get(sid)
    if s:
        return {
            "title": s.get("title") or "",
            "icon": s.get("icon") or "message-square",
            "agent_id": s.get("agent_id"),
            "provider": s.get("model_provider") or "",
            "model": s.get("model_name") or "",
            "deleted": False,
        }
    found = _find_meta(sid)
    if found:
        m, _slug = found
        return {
            "title": m.get("title") or "",
            "icon": m.get("icon") or "message-square",
            "agent_id": m.get("agent_id"),
            "provider": m.get("provider") or "",
            "model": m.get("model") or "",
            "deleted": False,
        }
    return {
        "title": f"(已删除) {sid[:6]}",
        "icon": "message-square",
        "agent_id": None,
        "provider": "",
        "model": "",
        "deleted": True,
    }


@router.get("/api/usage/overview")
async def usage_overview(days: int = 30) -> dict:
    """KPI + daily series + provider/model breakdown for the trailing window
    (design §5). Window is clamped to [1, retention]."""
    days = max(1, min(int(days), usage_store.RETENTION_DAYS))
    return {"ok": True, **usage_store.aggregate_overview(days)}


@router.get("/api/usage/hourly")
async def usage_hourly(date: str | None = None) -> dict:
    """24-hour distribution for one day (defaults to today, local time)."""
    return {"ok": True, **usage_store.aggregate_hourly(date)}


@router.get("/api/usage/sessions")
async def usage_sessions(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    sort: str = "total",
    limit: int = 200,
) -> dict:
    """Per-session aggregates over [from, to] (defaults: full retention window
    ending today), joined with display meta."""
    rows = usage_store.aggregate_sessions(from_, to, sort=sort, limit=limit)
    for r in rows:
        sid = r.get("session_id")
        r.update(_usage_session_display(sid) if sid else {
            "title": "(后台/系统)", "icon": "cpu", "agent_id": None,
            "provider": "", "model": "", "deleted": False,
        })
    return {"ok": True, "sessions": rows}


@router.get("/api/usage/sessions/{session_id}")
async def usage_session_detail(
    session_id: str,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict:
    """One session's aggregate within [from, to] (defaults: full retention)."""
    totals = usage_store.session_totals(session_id, from_, to)
    if totals is None:
        return {"ok": True, "usage": None}
    return {"ok": True, "usage": totals, **_usage_session_display(session_id)}


@router.get("/api/usage/requests")
async def usage_requests(
    date: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Request log for one day with filters + pagination (design §3.4)."""
    res = usage_store.query_requests(
        date_str=date, provider=provider, model=model, source=source,
        session_id=session_id, page=page, page_size=page_size,
    )
    return {"ok": True, **res}
