"""Diagnostics endpoint (P4-1): a read-only health report for the app."""

from __future__ import annotations

from fastapi import APIRouter

from .. import diagnostics

router = APIRouter()


@router.get("/api/diagnostics")
async def get_diagnostics() -> dict:
    """Full self-check (data integrity / tasks / runtime / contracts).

    Read-only and on demand — the settings page renders it as a report. The
    lighter startup subset lives in ``/api/health``.
    """
    return diagnostics.run_diagnostics()
