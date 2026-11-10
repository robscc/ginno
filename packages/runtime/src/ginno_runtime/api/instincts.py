"""Instincts REST API（P0，docs/instincts-design.md §10.2 子集）。

列表 / 详情 / 创建 / 编辑 / 删除（软删默认）+ 观察 feed（只读）。
P1+ 的 promote/evolve/prune-scan/export 留位不实现。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..instincts import (
    Instinct,
    delete_instinct,
    get_instinct,
    list_instincts,
    recent_observations,
    save_instinct,
)
from ..instincts.types import DOMAINS, SOURCES

router = APIRouter(prefix="/api/instincts", tags=["instincts"])


class InstinctBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    trigger: str = Field(min_length=1)
    behavior: str = Field(min_length=1)
    confidence: float = 0.5
    domain: str = "tool-usage"
    scope: str = "project"
    project_slug: str = ""
    source: str = "manual"
    status: str = "pending"
    evidence: dict[str, Any] | None = None


@router.get("")
def api_list_instincts(
    scope: str | None = None,
    project_slug: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    min_confidence: float | None = None,
) -> list[dict[str, Any]]:
    return [i.to_yaml_dict() for i in list_instincts(
        scope=scope, project_slug=project_slug, status=status,
        domain=domain, min_confidence=min_confidence)]


@router.get("/observations")
def api_observations(limit: int = 50) -> list[dict[str, Any]]:
    return recent_observations(limit=max(1, min(limit, 500)))


@router.post("")
def api_create_instinct(body: InstinctBody) -> dict[str, Any]:
    _validate(body)
    if get_instinct(body.scope, body.project_slug, body.id):
        raise HTTPException(409, f"instinct '{body.id}' already exists")
    inst = _to_instinct(body)
    return save_instinct(inst).to_yaml_dict()


@router.get("/{instinct_id}")
def api_get_instinct(instinct_id: str, scope: str = "project", project_slug: str = "") -> dict[str, Any]:
    inst = get_instinct(scope, project_slug, instinct_id)
    if not inst:
        raise HTTPException(404, "instinct not found")
    return inst.to_yaml_dict()


@router.put("/{instinct_id}")
def api_update_instinct(instinct_id: str, body: InstinctBody) -> dict[str, Any]:
    _validate(body)
    old = get_instinct(body.scope, body.project_slug, instinct_id)
    if not old:
        raise HTTPException(404, "instinct not found")
    inst = _to_instinct(body)
    inst.created = old.created  # 编辑保创建时间
    return save_instinct(inst).to_yaml_dict()


@router.delete("/{instinct_id}")
def api_delete_instinct(
    instinct_id: str, scope: str = "project", project_slug: str = "", hard: bool = False
) -> dict[str, Any]:
    if not delete_instinct(scope, project_slug, instinct_id, hard=hard):
        raise HTTPException(404, "instinct not found")
    return {"ok": True}


def _validate(body: InstinctBody) -> None:
    if body.domain not in DOMAINS:
        raise HTTPException(422, f"domain must be one of {DOMAINS}")
    if body.source not in SOURCES:
        raise HTTPException(422, f"source must be one of {SOURCES}")
    if body.scope not in ("project", "global"):
        raise HTTPException(422, "scope must be project|global")
    if body.scope == "project" and not body.project_slug:
        raise HTTPException(422, "project scope requires project_slug")
    if not 0.0 <= body.confidence <= 1.0:
        raise HTTPException(422, "confidence out of range")


def _to_instinct(body: InstinctBody) -> Instinct:
    ev = body.evidence or {}
    return Instinct(
        id=body.id,
        trigger=body.trigger,
        behavior=body.behavior,
        confidence=body.confidence,
        domain=body.domain,
        scope=body.scope,
        project_slug=body.project_slug,
        source=body.source,
        status=body.status,
        evidence={
            "observed": int(ev.get("observed", 0)),
            "applied": int(ev.get("applied", 0)),
            "uncorrected": int(ev.get("uncorrected", 0)),
            "corrected": int(ev.get("corrected", 0)),
            "last_seen": str(ev.get("last_seen", "")),
        },
    )
