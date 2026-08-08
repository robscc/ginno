"""Memory endpoints (MEMORY.md + pool summarization, P2)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import paths

router = APIRouter()


@router.get("/api/memory")
async def get_memory() -> dict:
    """Return MEMORY.md content + pool count."""
    from ..memory import pool_count

    p = paths.memory_index_path()
    content = p.read_text(encoding="utf-8") if p.exists() else ""
    return {"ok": True, "content": content, "pool_count": pool_count()}


@router.post("/api/memory/summarize")
async def post_memory_summarize(data: dict | None = None) -> dict:
    """Trigger memory summarization (pool → MEMORY.md via LLM)."""
    from ..memory import summarize_pool

    provider = (data or {}).get("provider")
    return await summarize_pool(model_provider=provider)
