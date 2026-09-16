"""Memory endpoints (MEMORY.md + gated distillation: draft → review → apply)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import paths

router = APIRouter()


async def _notify_memory_changed(draft: bool) -> None:
    """Tell every open UI that memory state changed (badge + panel refresh)."""
    from ..server_shared import _push_global_event

    try:
        await _push_global_event("memory.changed", {"draft": draft})
    except Exception:
        pass


@router.get("/api/memory")
async def get_memory() -> dict:
    """Return MEMORY.md content + pool count + pending-draft/KB status."""
    from ..memory import has_draft, pool_count

    p = paths.memory_index_path()
    content = p.read_text(encoding="utf-8") if p.exists() else ""
    try:
        from ..knowledge.config import load_knowledge_config

        kb_usable = load_knowledge_config().usable
    except Exception:
        kb_usable = False
    return {
        "ok": True,
        "content": content,
        "pool_count": pool_count(),
        "draft_pending": has_draft(),
        "kb_usable": kb_usable,
    }


@router.post("/api/memory/summarize")
async def post_memory_summarize(data: dict | None = None) -> dict:
    """Distill the pool into a pending DRAFT (never overwrites MEMORY.md).

    The user reviews the diff and applies/discards via /api/memory/draft/*.
    """
    from ..memory import create_draft

    provider = (data or {}).get("provider")
    result = await create_draft(trigger="manual", provider=provider)
    if result.get("ok") and not result.get("draft_exists") and result.get("draft"):
        await _notify_memory_changed(draft=True)
    return result


@router.get("/api/memory/draft")
async def get_memory_draft() -> dict:
    """Pending draft review payload (survives restarts), or ``draft: null``."""
    from ..memory import draft_payload, has_draft

    if not has_draft():
        return {"ok": True, "draft": None}
    return {"ok": True, **draft_payload()}


@router.post("/api/memory/draft/apply")
async def post_memory_draft_apply(data: dict | None = None) -> dict:
    """Write the reviewed (optionally edited) draft into MEMORY.md and clear
    the distilled pool entries."""
    from ..memory import apply_draft

    body = data or {}
    content = body.get("content")
    force = bool(body.get("force"))
    result = apply_draft(content=content, force=force)
    if result.get("ok"):
        await _notify_memory_changed(draft=False)
    return result


@router.post("/api/memory/draft/discard")
async def post_memory_draft_discard() -> dict:
    """Drop the pending draft; the pool is kept."""
    from ..memory import discard_draft

    result = discard_draft()
    await _notify_memory_changed(draft=False)
    return result
