"""Instincts REST API（P0，docs/instincts-design.md §10.2 子集）。

列表 / 详情 / 创建 / 编辑 / 删除（软删默认）+ 观察 feed（只读）。
P2 evolve：聚类预览 + 沉淀 SKILL.md（instincts/evolve.py）。
promote/prune-scan/export 仍留位。
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


class EvolveBody(BaseModel):
    scope: str = "project"
    project_slug: str = ""
    domain: str = "workflow"

class IdsBody(BaseModel):
    ids: list[str] = []


class ResolveBody(BaseModel):
    keep_id: str
    drop_id: str



@router.get("/evolve/clusters")
def api_evolve_clusters() -> list[dict[str, Any]]:
    """可 evolve 的聚类预览（同域 ≥3 条 active 且 confidence ≥0.7）。"""
    from ..instincts.evolve import find_clusters

    return find_clusters()


@router.post("/evolve")
def api_evolve(body: EvolveBody) -> dict[str, Any]:
    """把一个聚类沉淀为 SKILL.md 草稿；源规则置 evolved（不再注入）。"""
    from ..instincts.evolve import evolve_cluster

    if body.domain not in DOMAINS:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "bad domain"})
    r = evolve_cluster(body.scope, body.project_slug, body.domain)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


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


# ── P1 收尾：冲突区 + export/import（2026-11-12）──────────────────────


@router.get("/conflicts")
def api_conflicts(project_slug: str = "") -> list[dict[str, Any]]:
    """确定性冲突对扫描（trigger 重叠 ≥0.6 且 behavior 肯定/否定对立）。"""
    from ..instincts.store import find_conflicts

    pairs = find_conflicts(project_slug=project_slug)
    # 附带双方摘要，前端冲突区直接渲染
    out = []
    for p in pairs:
        a = get_instinct_by_id(p["a"])
        b = get_instinct_by_id(p["b"])
        if a and b:
            out.append({
                "a": a.to_yaml_dict(), "b": b.to_yaml_dict(),
                "trigger_overlap": p["trigger_overlap"],
            })
    return out


@router.post("/conflicts/freeze")
def api_conflicts_freeze(body: IdsBody) -> dict[str, Any]:
    """把冲突对双方置 conflict（冻结注入），待用户裁决。"""
    from ..instincts.store import mark_conflict

    n = mark_conflict(body.ids or [])
    return {"ok": True, "frozen": n}


@router.post("/conflicts/resolve")
def api_conflicts_resolve(body: ResolveBody) -> dict[str, Any]:
    """裁决：keep_id 回 active、drop_id 置 pruned。"""
    from ..instincts.store import resolve_conflict

    ok = resolve_conflict(body.keep_id, body.drop_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "not found"})
    return {"ok": True}


@router.get("/export")
def api_export() -> dict[str, Any]:
    """全部条目导出（跨机迁移用；YAML 单文件语义保留为 dict 列表）。"""
    items = []
    for st in ("active", "pending", "pruned", "evolved", "conflict"):
        items.extend(i.to_yaml_dict() for i in list_instincts(status=st))
    return {"version": 1, "exported_at": _now_iso(), "instincts": items}


@router.post("/import")
def api_import(body: dict[str, Any]) -> dict[str, Any]:
    """导入（合并语义）：新 id 直接落盘；已有 id 仅当 updated 更新才覆盖。"""
    incoming = body.get("instincts") or []
    added = updated = skipped = 0
    for raw in incoming:
        try:
            inst = Instinct.from_yaml_dict(raw)
        except Exception:
            skipped += 1
            continue
        cur = get_instinct_by_id(inst.id)
        if cur is None:
            save_instinct(inst)
            added += 1
        elif (inst.updated or "") > (cur.updated or ""):
            save_instinct(inst)
            updated += 1
        else:
            skipped += 1
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}


def get_instinct_by_id(id_: str):
    """id 全局唯一：扫全部 status 目录定位（active/pending/pruned/…）。"""
    for st in ("active", "pending", "conflict", "evolved", "pruned"):
        for i in list_instincts(status=st):
            if i.id == id_:
                return i
    return None


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")
