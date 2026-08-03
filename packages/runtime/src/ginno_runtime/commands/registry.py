"""Built-in slash command registry.

Built-ins short-circuit the turn: the handler renders a reply directly and the
graph never runs (no LLM call, no checkpoint write). V1 ships only ``/help``;
``/new``, ``/clear`` etc. are reserved names for future built-ins.

Skills are NOT registered here — they live on disk (SKILL.md) and are resolved
by the resolver via ``SkillLoader``. Names collide → built-in wins (documented
in docs/commands-and-mentions-design.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..skills.loader import SkillLoader


@dataclass(frozen=True)
class BuiltinCommand:
    name: str
    description: str
    handler: Callable[[str | None], str]  # (project_slug) -> reply text


def _help_handler(project_slug: str | None = None) -> str:
    lines = ["**可用命令**", "", "内置命令："]
    for name in sorted(BUILTINS):
        c = BUILTINS[name]
        lines.append(f"- `/{name}` — {c.description}")
    skills = [
        s
        for s in SkillLoader(project_slug=project_slug).load()
        if s.trigger in ("user-invocable", "both")
    ]
    if skills:
        lines += ["", "技能（`/技能名 [prompt]` 调用）："]
        for s in sorted(skills, key=lambda x: x.name):
            desc = (s.description or "").strip()
            lines.append(f"- `/{s.name}`" + (f" — {desc}" if desc else ""))
    else:
        lines += ["", "（暂无可用技能，可在 设置 → Skills 添加）"]
    return "\n".join(lines)


BUILTINS: dict[str, BuiltinCommand] = {
    "help": BuiltinCommand(
        name="help",
        description="列出可用命令与技能",
        handler=_help_handler,
    ),
}
