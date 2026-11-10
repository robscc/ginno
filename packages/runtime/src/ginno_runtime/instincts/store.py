"""Instincts 存储：YAML 一文件一条 + 状态目录（docs/instincts-design.md §4.2）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import yaml

from ..paths import home
from .types import Instinct

_STATUS_DIRS = ("active", "pending", "pruned", "evolved")


def instincts_root() -> "Any":
    return home() / "instincts"


def instincts_dir_for(scope: str, project_slug: str, status: str = "active") -> Any:
    """scope=global → instincts/global/<status>/；project → instincts/projects/<slug>/<status>/。"""
    if status not in _STATUS_DIRS:
        raise ValueError(f"unknown status: {status}")
    if scope == "global":
        return instincts_root() / "global" / status
    return instincts_root() / "projects" / (project_slug or "default") / status


def observations_path() -> Any:
    return instincts_root() / "observations.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _dump(inst: Instinct) -> str:
    d = inst.to_yaml_dict()
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


def _load(path: Any) -> Instinct:
    d = yaml.safe_load(path.read_text() or "{}")
    return Instinct.from_yaml_dict(d or {})


def save_instinct(inst: Instinct) -> Instinct:
    """新建或更新（更新时目录随 status 变化迁移；pruned 软删同机制）。"""
    inst.updated = _now()
    if not inst.created:
        inst.created = inst.updated
    target_dir = instincts_dir_for(inst.scope, inst.project_slug, inst.status)
    target_dir.mkdir(parents=True, exist_ok=True)
    # 状态迁移：把旧状态目录里的同名文件挪走（save 主动调，先扫四目录）。
    for st in _STATUS_DIRS:
        if st == inst.status:
            continue
        old = instincts_dir_for(inst.scope, inst.project_slug, st) / f"{inst.id}.yaml"
        if old.exists():
            old.unlink()
    (target_dir / f"{inst.id}.yaml").write_text(_dump(inst), encoding="utf-8")
    return inst


def delete_instinct(scope: str, project_slug: str, id_: str, hard: bool = False) -> bool:
    """默认软删（移 pruned/）；hard=True 物理删除。"""
    for st in _STATUS_DIRS:
        p = instincts_dir_for(scope, project_slug, st) / f"{id_}.yaml"
        if not p.exists():
            continue
        if not hard:
            inst = _load(p)
            inst.status = "pruned"
            save_instinct(inst)
        else:
            p.unlink()
        return True
    return False


def get_instinct(scope: str, project_slug: str, id_: str) -> Instinct | None:
    for st in _STATUS_DIRS:
        p = instincts_dir_for(scope, project_slug, st) / f"{id_}.yaml"
        if p.exists():
            return _load(p)
    return None


def list_instincts(
    *,
    scope: str | None = None,
    project_slug: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    min_confidence: float | None = None,
) -> list[Instinct]:
    """跨 global + 各 project 扫描（管理页/API 用；注入走 inject 模块的精简路径）。"""
    roots = []
    g = instincts_root() / "global"
    if g.exists():
        roots.append(("global", "", g))
    proj_root = instincts_root() / "projects"
    if proj_root.exists():
        for d in sorted(proj_root.iterdir()):
            if d.is_dir():
                roots.append(("project", d.name, d))
    out: list[Instinct] = []
    for sc, slug, root in roots:
        if scope and sc != scope:
            continue
        if project_slug and slug != project_slug:
            continue
        for st in _STATUS_DIRS:
            if status and st != status:
                continue
            d = root / st
            if not d.exists():
                continue
            for f in sorted(d.glob("*.yaml")):
                try:
                    inst = _load(f)
                except Exception:
                    continue
                if domain and inst.domain != domain:
                    continue
                if min_confidence is not None and inst.confidence < min_confidence:
                    continue
                out.append(inst)
    out.sort(key=lambda i: (-i.confidence, i.id))
    return out
