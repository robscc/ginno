"""Memory endpoints (MEMORY.md + gated distillation: draft → review → apply).

The Settings → 智能体 → 记忆 page needs more than "is a draft pending": it shows
what memory is in effect this turn, the draft queue's history, pool samples and
run log entries (docs/dream-mode-design.md §6). The original single-draft
endpoints stay as aliases for one version so older web bundles keep working.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import paths
from .. import usage_store

router = APIRouter()


async def _notify_memory_changed(draft: bool) -> None:
    """Tell every open UI that memory state changed (badge + panel refresh)."""
    from ..server_shared import _push_global_event

    try:
        await _push_global_event("memory.changed", {"draft": draft})
    except Exception:
        pass


def _kb_usable() -> bool:
    try:
        from ..knowledge.config import load_knowledge_config

        return load_knowledge_config().usable
    except Exception:  # noqa: BLE001
        return False


def _auto_throttle() -> dict:
    """Automatic-path cooldown state (set by the turn hook after a failure).

    Surfaced so the page can say "cooling down until HH:MM" instead of leaving
    the user guessing why nothing runs.
    """
    try:
        from .stream import effects

        until = float(getattr(effects, "_AUTO_DISTILL_FAIL_AT", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        until = 0.0
    return {"until": until, "active": until > time.time()}


@router.get("/api/memory")
async def get_memory(agent_id: str | None = None) -> dict:
    """MEMORY.md content + pool + draft queue + the memory in effect."""
    from ..memory import draft_summary, has_draft, list_drafts, memory_status, pool_count

    p = paths.memory_index_path()
    content = p.read_text(encoding="utf-8") if p.exists() else ""
    pending = [draft_summary(m) for m in list_drafts(status="pending")]
    return {
        "ok": True,
        "content": content,
        "pool_count": pool_count(),
        "draft_pending": has_draft(),
        "drafts_pending": pending,
        "kb_usable": _kb_usable(),
        "status": memory_status(agent_id),
        "throttle": _auto_throttle(),
    }


@router.get("/api/memory/status")
async def get_memory_status(agent_id: str | None = None) -> dict:
    """Prompt-view projection only (cheap refresh target for the page)."""
    from ..memory import memory_status

    return {"ok": True, **memory_status(agent_id)}


@router.put("/api/memory")
async def put_memory(data: dict | None = None) -> dict:
    """Replace MEMORY.md wholesale (the page's 编辑 MEMORY.md path)."""
    from ..memory import memory_status, sanitize_for_memory

    body = data or {}
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": "content required"}
        )
    paths.memory_index_path().write_text(
        sanitize_for_memory(content), encoding="utf-8"
    )
    await _notify_memory_changed(draft=False)
    return {"ok": True, "status": memory_status(body.get("agent_id"))}


@router.post("/api/memory/sections/remove")
async def post_memory_section_remove(data: dict | None = None) -> dict:
    """Delete exact lines from MEMORY.md (a section the user no longer wants)."""
    from ..memory import memory_status, remove_memory_lines

    body = data or {}
    lines = body.get("lines")
    if not isinstance(lines, list) or not lines:
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": "lines required"}
        )
    removed = remove_memory_lines([str(x) for x in lines])
    await _notify_memory_changed(draft=False)
    return {"ok": True, "removed": removed, "status": memory_status(body.get("agent_id"))}


@router.post("/api/memory/summarize")
async def post_memory_summarize(data: dict | None = None) -> dict:
    """Distill the pool into a pending DRAFT (never overwrites MEMORY.md).

    The user reviews the diff and applies/discards via /api/memory/drafts/*.
    """
    from ..memory import create_draft

    body = data or {}
    result = await create_draft(
        trigger=str(body.get("trigger") or "manual"),
        provider=body.get("provider"),
    )
    if result.get("ok") and not result.get("draft_exists") and result.get("draft"):
        await _notify_memory_changed(draft=True)
    return result


# ---- draft queue -----------------------------------------------------------


@router.get("/api/memory/drafts")
async def get_memory_drafts(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    from ..memory import draft_summary, list_drafts

    rows = [draft_summary(m) for m in list_drafts(status=status, limit=limit)]
    return {"ok": True, "drafts": rows}


def _draft_or_404(draft_id: str) -> dict:
    from ..memory import draft_payload

    payload = draft_payload(draft_id)
    if not payload.get("draft"):
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "draft not found"}
        )
    return payload


@router.get("/api/memory/drafts/{draft_id}")
async def get_memory_draft_detail(draft_id: str) -> dict:
    return {"ok": True, **_draft_or_404(draft_id)}


@router.post("/api/memory/drafts/{draft_id}/apply")
async def post_memory_draft_apply_by_id(draft_id: str, data: dict | None = None) -> dict:
    """Write the reviewed (optionally edited, optionally truncated) draft."""
    from ..memory import apply_draft

    body = data or {}
    result = apply_draft(
        content=body.get("content"),
        force=bool(body.get("force")),
        draft_id=draft_id,
        truncate=bool(body.get("truncate")),
    )
    if result.get("ok"):
        await _notify_memory_changed(draft=False)
    return result


@router.post("/api/memory/drafts/{draft_id}/discard")
async def post_memory_draft_discard_by_id(draft_id: str) -> dict:
    from ..memory import discard_draft

    result = discard_draft(draft_id)
    await _notify_memory_changed(draft=False)
    return result


@router.post("/api/memory/drafts/{draft_id}/split")
async def post_memory_draft_split(draft_id: str, data: dict | None = None) -> dict:
    """Keep a subset of the draft's sections as a new pending draft.

    The over-budget escape hatch: whole ``##`` sections instead of a mid-line
    truncation. The original draft is marked discarded; no LLM call is made.
    """
    from ..memory import split_draft

    body = data or {}
    keep = body.get("keep")
    if not isinstance(keep, list) or not keep:
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": "keep required"}
        )
    result = split_draft(draft_id, [str(h) for h in keep])
    if result.get("ok"):
        await _notify_memory_changed(draft=True)
    return result


@router.get("/api/memory/trend")
async def get_memory_trend(
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Daily pool-watermark / memory-budget snapshots (oldest first)."""
    from ..memory import memory_trend

    return {"ok": True, "days": days, "trend": memory_trend(days)}


@router.delete("/api/memory/drafts/expired")
async def delete_expired_drafts() -> dict:
    """Drop expired/discarded rows from the queue view (drafts already resolved)."""
    from ..memory import list_drafts

    removed = []
    for meta in list_drafts():
        if meta.get("status") in ("expired", "discarded"):
            for p in (
                paths.memory_drafts_dir() / f"{meta['id']}.md",
                paths.memory_drafts_dir() / f"{meta['id']}.json",
            ):
                try:
                    p.unlink(missing_ok=True)
                    removed.append(meta["id"])
                except OSError:
                    pass
    return {"ok": True, "removed": sorted(set(removed))}


# ---- legacy single-draft aliases (one version) -----------------------------


@router.get("/api/memory/draft")
async def get_memory_draft() -> dict:
    """Latest pending draft payload (alias kept for older web bundles)."""
    from ..memory import draft_payload, has_draft

    if not has_draft():
        return {"ok": True, "draft": None}
    return {"ok": True, **draft_payload()}


@router.post("/api/memory/draft/apply")
async def post_memory_draft_apply(data: dict | None = None) -> dict:
    from ..memory import apply_draft

    body = data or {}
    result = apply_draft(
        content=body.get("content"),
        force=bool(body.get("force")),
        truncate=bool(body.get("truncate")),
    )
    if result.get("ok"):
        await _notify_memory_changed(draft=False)
    return result


@router.post("/api/memory/draft/discard")
async def post_memory_draft_discard() -> dict:
    from ..memory import discard_draft

    result = discard_draft()
    await _notify_memory_changed(draft=False)
    return result


# ---- pool ------------------------------------------------------------------


@router.get("/api/memory/pool")
async def get_memory_pool(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Pool samples. Bodies are truncated — the page browses, it does not read."""
    from ..memory import read_pool

    entries = read_pool()
    total = len(entries)
    cited = sum(1 for e in entries if e.get("cited"))
    stamps = [float(e.get("timestamp") or 0) for e in entries if e.get("timestamp")]
    page = entries[offset : offset + limit]
    return {
        "ok": True,
        "total": total,
        "cited": cited,
        "first_ts": min(stamps) if stamps else None,
        "last_ts": max(stamps) if stamps else None,
        "entries": [
            {
                "timestamp": e.get("timestamp"),
                "session_id": e.get("session_id"),
                "agent_id": e.get("agent_id"),
                "cited": bool(e.get("cited")),
                "chars": len(e.get("content") or ""),
                "excerpt": (e.get("content") or "")[:200],
            }
            for e in page
        ],
    }


@router.delete("/api/memory/pool")
async def delete_memory_pool(data: dict | None = None) -> dict:
    """Empty the pool. Requires an explicit ``confirm`` flag."""
    from ..memory import clear_pool, pool_count

    if not (data or {}).get("confirm"):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "confirm required"},
        )
    before = pool_count()
    clear_pool()
    return {"ok": True, "removed": before}


# ---- runs + usage ----------------------------------------------------------


@router.get("/api/memory/runs")
async def get_memory_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    from ..memory import run_history

    return {"ok": True, "runs": run_history(limit=limit), "throttle": _auto_throttle()}


@router.post("/api/memory/runs/{run_id}/retry")
async def post_memory_run_retry(run_id: str) -> dict:
    """Re-run distillation now, bypassing the automatic path's cooldown.

    The run id is echoed back so the UI can point at the retry of a specific
    failed row; the new attempt gets its own run id (runs are append-only).
    """
    from ..memory import create_draft

    result = await create_draft(trigger="manual")
    if result.get("ok") and not result.get("draft_exists") and result.get("draft"):
        await _notify_memory_changed(draft=True)
    return {"ok": bool(result.get("ok")), "retried_from": run_id, **result}


@router.get("/api/memory/usage")
async def get_memory_usage(days: int = Query(default=7, ge=1, le=90)) -> dict:
    """Distillation cost from the usage ledger (source="memory").

    Same source of truth as Settings → 用量统计, so the numbers here and the
    「记忆」bucket there can never drift.
    """
    today = time.strftime("%Y-%m-%d", time.localtime())
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
    rows = [
        e
        for e in usage_store.iter_range(start, today)
        if (e.get("source") or "") == "memory"
    ]
    return {
        "ok": True,
        "days": days,
        "calls": len(rows),
        "input_tokens": sum(int(e.get("input_tokens") or 0) for e in rows),
        "output_tokens": sum(int(e.get("output_tokens") or 0) for e in rows),
        "cache_read_tokens": sum(int(e.get("cache_read_tokens") or 0) for e in rows),
        "failed": sum(1 for e in rows if not e.get("ok", True)),
    }
