"""Instincts evolve：同域高置信规则聚类 → 起草 SKILL.md（设计 §9，P2）。

确定性聚类（无 LLM）：按 (scope, project_slug, domain) 分组，组内
confidence ≥ 0.7 且 status=active 的规则 ≥ 3 条 → 可 evolve。产物落
``~/.ginno/skills/<name>/SKILL.md``（与 distill kind=skill 同一落点），
源 instinct 置 ``status=evolved`` + ``evolved_to=<skill name>``（不再注入，
由 skill 承载）。幂等：已 evolved 的不重复参与；同名 skill 追加后缀。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from .. import paths
from . import store
from .types import Instinct

MIN_CLUSTER = 3
MIN_CONFIDENCE = 0.7


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "instincts"


def find_clusters() -> list[dict[str, Any]]:
    """扫描可 evolve 的聚类（只读，管理页预览用）。"""
    groups: dict[tuple, list[Instinct]] = defaultdict(list)
    for inst in store.list_instincts(status="active", min_confidence=MIN_CONFIDENCE):
        groups[(inst.scope, inst.project_slug, inst.domain)].append(inst)
    out = []
    for (scope, slug, domain), members in sorted(groups.items()):
        if len(members) >= MIN_CLUSTER:
            out.append(
                {
                    "scope": scope,
                    "project_slug": slug,
                    "domain": domain,
                    "count": len(members),
                    "ids": [m.id for m in members],
                    "avg_confidence": round(
                        sum(m.confidence for m in members) / len(members), 3
                    ),
                }
            )
    return out


def _skill_md(name: str, domain: str, members: list[Instinct]) -> str:
    today = time.strftime("%Y-%m-%d")
    lines = [
        "---",
        f"name: {name}",
        f"description: 从 {len(members)} 条高置信本能规则沉淀的 {domain} 技能草稿，使用前请按实际需要调整。",
        "trigger: user-invocable",
        "---",
        "",
        f"# {name}",
        "",
        f"> 由 instincts evolve 于 {today} 沉淀（{domain} 域，{len(members)} 条规则，"
        f"平均置信度 {sum(m.confidence for m in members) / len(members):.2f}）。",
        "",
        "## 行为规则",
        "",
    ]
    for i, m in enumerate(members, 1):
        lines.append(f"{i}. **当** {m.trigger}")
        lines.append(f"   **则** {m.behavior}")
        lines.append("")
    lines += [
        "## 完成标准",
        "",
        "按上述规则执行；规则冲突时以置信度高者优先，遇到输入缺失先询问用户。",
        "",
    ]
    return "\n".join(lines)


def evolve_cluster(scope: str, project_slug: str, domain: str) -> dict[str, Any]:
    """把一个聚类沉淀为 SKILL.md 草稿；源规则置 evolved。"""
    members = [
        m
        for m in store.list_instincts(
            scope=scope, project_slug=project_slug, domain=domain,
            status="active", min_confidence=MIN_CONFIDENCE,
        )
    ]
    if len(members) < MIN_CLUSTER:
        return {"ok": False, "error": f"cluster too small ({len(members)} < {MIN_CLUSTER})"}

    base = _slug(f"instincts-{domain}-{project_slug or 'global'}")
    name = base
    target = paths.global_skills_dir() / name / "SKILL.md"
    n = 2
    while target.exists():
        name = f"{base}-{n}"
        target = paths.global_skills_dir() / name / "SKILL.md"
        n += 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_skill_md(name, domain, members), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"write failed: {e}"}

    for m in members:
        m.status = "evolved"
        m.evolved_to = name
        store.save_instinct(m)

    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "evolved_ids": [m.id for m in members],
    }
