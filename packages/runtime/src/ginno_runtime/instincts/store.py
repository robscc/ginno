"""Instincts 存储：YAML 一文件一条 + 状态目录（docs/instincts-design.md §4.2）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import yaml

from ..paths import home
from .types import Instinct

_STATUS_DIRS = ("active", "pending", "pruned", "evolved", "conflict")


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

# ── 冲突检测（§6「矛盾→conflict」冻结，P1 收尾）──────────────────────
# 确定性（零 LLM）：同 scope+project+domain 的 active 条目中，trigger
# 归一化后 token 集重叠 ≥0.6 且 behavior 一方强肯定、一方强否定 → 冲突。
# 双方置 status=conflict（inject 只取 active → 天然冻结）；管理页冲突区
# 供用户裁决（保留一方 → 另一方 pruned，保留方回 active）。

_POS = ("always", "use ", "prefer", "要 ", "应该", "务必", "直接用", "prefer")
_NEG = ("never", "avoid", "don't", "do not", "不要", "禁止", "避免", "别用", "stop using")


def _norm_tokens(text: str) -> set[str]:
    import re as _re
    return set(w for w in _re.split(r"[^\w\u4e00-\u9fff]+", text.lower()) if len(w) > 1)


def _polarity(behavior: str) -> int:
    """1=肯定式（做什么）、-1=否定式（别做什么）、0=不明。"""
    b = behavior.lower()
    if any(k in b for k in _NEG):
        return -1
    if any(k in b for k in _POS):
        return 1
    return 0


def find_conflicts(project_slug: str = "") -> list[dict[str, Any]]:
    """扫描 active 条目返回冲突对 [{a, b, trigger_overlap}]；不落盘。"""
    items = [i for i in list_instincts(status="active")
             if not project_slug or i.project_slug == project_slug]
    out: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for x in range(len(items)):
        for y in range(x + 1, len(items)):
            a, b = items[x], items[y]
            if (a.scope, a.project_slug, a.domain) != (b.scope, b.project_slug, b.domain):
                continue
            ta, tb = _norm_tokens(a.trigger), _norm_tokens(b.trigger)
            if not ta or not tb:
                continue
            overlap = len(ta & tb) / min(len(ta), len(tb))
            if overlap < 0.6:
                continue
            if _polarity(a.behavior) * _polarity(b.behavior) < 0:
                key = tuple(sorted((a.id, b.id)))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                out.append({"a": a.id, "b": b.id, "trigger_overlap": round(overlap, 2)})
    return out


def mark_conflict(ids: list[str]) -> int:
    """把给定 id 的 active 条目置 conflict（返回成功数）。

    id 全局唯一（文件名）：扫所有 scope/project 的 active 目录定位。
    """
    n = 0
    paths = list(instincts_root().glob("global/active/*.yaml")) + list(
        instincts_root().glob("projects/*/active/*.yaml")
    )
    for yaml_path in paths:
        if yaml_path.stem not in ids:
            continue
        inst = _load(yaml_path)
        if inst.status != "active":
            continue
        inst.status = "conflict"
        inst.updated = _now()
        save_instinct(inst)
        n += 1
    return n


def resolve_conflict(keep_id: str, drop_id: str) -> bool:
    """用户裁决冲突对：keep 回 active、drop 置 pruned。返回是否成功。"""
    ok = False
    paths = list(instincts_root().glob("global/conflict/*.yaml")) + list(
        instincts_root().glob("projects/*/conflict/*.yaml")
    )
    for yaml_path in paths:
        inst = _load(yaml_path)
        if inst.id == keep_id:
            inst.status = "active"
            inst.updated = _now()
            save_instinct(inst)
            ok = True
        elif inst.id == drop_id:
            inst.status = "pruned"
            inst.updated = _now()
            save_instinct(inst)
            ok = True
    return ok
