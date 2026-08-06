"""SKILL.md loader.

Skill format (Claude-Code-inspired):

    <package>/skills/builtin/<name>/SKILL.md         (built-in, ships with Ginno)
    ~/.ginno/skills/<name>/SKILL.md                  (global, user-installed)
    ~/.ginno/projects/<slug>/skills/<name>/SKILL.md  (project-scoped override)

Precedence on name conflict: builtin < global < project (a user copy
overrides a built-in without breaking it; built-ins can't be deleted).

    ---
    name: summarize-notes
    description: Summarize a set of Obsidian notes into a brief
    trigger: user-invocable       # or model-invocable | both
    tools: [read, write]
    ---

    # Skill body — instructions injected when triggered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import paths

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    trigger: str = "both"  # user-invocable | model-invocable | both
    allowed_tools: list[str] = field(default_factory=list)
    # Declares this skill as the sync adapter for an external TODO platform
    # (todo-provider design); makes it show up in todo provider discovery.
    todo_provider: str = ""
    body: str = ""
    path: Path | None = None
    builtin: bool = False  # shipped with Ginno; not deletable via the API

    def system_prompt_snippet(self) -> str:
        """One-line summary for injection into system prompt index."""
        return f"- {self.name}: {self.description}"


def _parse_skill_file(p: Path, builtin: bool = False) -> Skill | None:
    raw = p.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    return Skill(
        name=meta.get("name", p.parent.name),
        description=meta.get("description", ""),
        trigger=meta.get("trigger", "both"),
        allowed_tools=meta.get("tools", []) or [],
        todo_provider=str(meta.get("todo_provider") or ""),
        body=body,
        path=p,
        builtin=builtin,
    )


def _scan_dir(root: Path, builtin: bool, into: dict[str, Skill]) -> None:
    if not root.exists():
        return
    for p in root.glob("*/SKILL.md"):
        s = _parse_skill_file(p, builtin=builtin)
        if s:
            into[s.name] = s


def load_all_skills(project_slug: str | None = None) -> list[Skill]:
    """Load builtin + global + project skills. Later tiers win on conflict
    (builtin < global < project), so a user copy overrides a built-in."""
    skills: dict[str, Skill] = {}

    _scan_dir(paths.builtin_skills_dir(), builtin=True, into=skills)
    _scan_dir(paths.global_skills_dir(), builtin=False, into=skills)
    if project_slug:
        _scan_dir(paths.project_skills_dir(project_slug), builtin=False, into=skills)

    return list(skills.values())


@dataclass
class SkillLoader:
    project_slug: str | None = None

    def load(self) -> list[Skill]:
        return load_all_skills(self.project_slug)

    def build_index_prompt(self) -> str:
        skills = self.load()
        if not skills:
            return ""
        lines = [
            "Available skills (the user can invoke one by starting a message with"
            " /<name>; listed here for your awareness):"
        ]
        lines += [s.system_prompt_snippet() for s in skills]
        return "\n".join(lines)

    def get(self, name: str) -> Skill | None:
        for s in self.load():
            if s.name == name:
                return s
        return None
