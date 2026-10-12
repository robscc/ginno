"""Plans HTTP API — Settings/board 的读面 + 用户确认/取消（设计 §2/P3）.

主写入面是 agent 工具（tools/plan_tools.py）；这里的 POST 只服务用户侧
动作（确认一张 needs_confirm 清单、取消清单）。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..plans import store, watcher
from ..server_shared import _log
from ..session_meta import _find_meta

router = APIRouter()


def _slug_of(session_id: str) -> str:
    meta, _ = _find_meta(session_id) or ({}, None)
    return (meta or {}).get("project_slug") or "default"


@router.get("/api/sessions/{session_id}/plans")
async def list_plans_endpoint(session_id: str) -> list[dict]:
    return store.list_plans(_slug_of(session_id), session_id)


@router.get("/api/sessions/{session_id}/plans/{plan_id}")
async def get_plan_endpoint(session_id: str, plan_id: str) -> dict:
    plan = store.get_plan(_slug_of(session_id), session_id, plan_id)
    return plan or {"error": "not found"}


@router.post("/api/sessions/{session_id}/plans/{plan_id}/confirm")
async def confirm_plan_endpoint(session_id: str, plan_id: str) -> dict:
    plan = store.confirm_plan(_slug_of(session_id), session_id, plan_id)
    if plan is None:
        return {"ok": False, "error": "plan not found or not awaiting confirmation"}
    watcher.notify_change(session_id)
    _log.info("plan_confirmed session=%s plan=%s", session_id, plan_id)
    return {"ok": True, "plan": plan}


@router.post("/api/sessions/{session_id}/plans/{plan_id}/cancel")
async def cancel_plan_endpoint(session_id: str, plan_id: str) -> dict:
    plan = store.cancel_plan(_slug_of(session_id), session_id, plan_id)
    if plan is None:
        return {"ok": False, "error": "plan not found or already terminal"}
    watcher.notify_change(session_id)
    _log.info("plan_cancelled session=%s plan=%s", session_id, plan_id)
    return {"ok": True, "plan": plan}
