"""Config health-check endpoint ("Doctor", 2026-11-19).

GET /api/doctor — read-only sweep over the five config surfaces (mcp /
skills / hooks / settings / runtime). Contract in ``ginno_runtime.doctor``;
the frontend Doctor page renders it (i18n keyed by check id). No caching:
the page is opened explicitly and a stale verdict after a fix would defeat
the diagnosis.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import doctor

router = APIRouter()


@router.get("/api/doctor")
async def get_doctor() -> dict:
    return await doctor.run_doctor()
