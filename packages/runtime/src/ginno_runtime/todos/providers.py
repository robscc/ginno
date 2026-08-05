"""TODO platform provider discovery + resolution.

A *provider* is a logical id (free-form string, never an enum) for an external
TODO platform (DingTalk, a TODO-list service, …). Capabilities come from two
sources, merged with settings winning:

* skill declaration — a SKILL.md frontmatter key ``todo_provider: <id>``
* user config      — ``settings.json → todo_providers: {<id>: {label, skill?,
  mcp?, auto_push?}}``

Sync itself is LLM-driven (todo-pull / todo-push workflows inject the
provider's skill), so adding a platform needs no adapter code.
"""

from __future__ import annotations

import json
from typing import Any

from .. import paths
from ..skills.loader import SkillLoader


def _settings() -> dict[str, Any]:
    try:
        return json.loads(paths.settings_path().read_text() or "{}")
    except Exception:
        return {}


def list_todo_providers(project_slug: str = "default") -> list[dict[str, Any]]:
    """All known TODO providers: skill declarations merged under settings."""
    out: dict[str, dict[str, Any]] = {}
    for s in SkillLoader(project_slug=project_slug).load():
        pid = (getattr(s, "todo_provider", "") or "").strip()
        if not pid:
            continue
        out[pid] = {
            "id": pid,
            "label": s.name,
            "skill": s.name,
            "mcp": None,
            "auto_push": True,
            "source": "skill",
        }
    cfg = _settings().get("todo_providers") or {}
    for pid, c in cfg.items():
        if not isinstance(c, dict):
            continue
        base = out.get(pid) or {
            "id": pid,
            "label": pid,
            "skill": None,
            "mcp": None,
            "auto_push": True,
            "source": "settings",
        }
        base.update({k: v for k, v in c.items() if v is not None})
        base["id"] = pid
        base.setdefault("label", pid)
        out[pid] = base
    return sorted(out.values(), key=lambda p: str(p["id"]))


def get_todo_provider(pid: str, project_slug: str = "default") -> dict[str, Any] | None:
    return next((p for p in list_todo_providers(project_slug) if p["id"] == pid), None)


def resolve_skill_for(pid: str, prov: dict[str, Any] | None = None, project_slug: str = "default") -> str | None:
    """Skill to inject for a provider sync run: config > declaration > name convention."""
    prov = prov if prov is not None else get_todo_provider(pid, project_slug)
    skill = (prov or {}).get("skill")
    if skill:
        return str(skill)
    if SkillLoader(project_slug=project_slug).get(pid):
        return pid  # convention: skill named after the provider id
    return None
