"""SKILL.md loader.

Skill format (Claude-Code-inspired):

    ~/.ginno/skills/<name>/SKILL.md
    ~/.ginno/projects/<slug>/skills/<name>/SKILL.md  (project-scoped override)

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
    body: str = ""
    path: Path | None = None

    def system_prompt_snippet(self) -> str:
        """One-line summary for injection into system prompt index."""
        return f"- {self.name}: {self.description}"


def _parse_skill_file(p: Path) -> Skill | None:
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
        body=body,
        path=p,
    )


def load_all_skills(project_slug: str | None = None) -> list[Skill]:
    """Load global skills + project-scoped overrides. Project wins on conflict."""
    skills: dict[str, Skill] = {}

    global_dir = paths.global_skills_dir()
    if global_dir.exists():
        for p in global_dir.glob("*/SKILL.md"):
            s = _parse_skill_file(p)
            if s:
                skills[s.name] = s

    if project_slug:
        proj_dir = paths.project_skills_dir(project_slug)
        if proj_dir.exists():
            for p in proj_dir.glob("*/SKILL.md"):
                s = _parse_skill_file(p)
                if s:
                    skills[s.name] = s

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
