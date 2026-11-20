"""Help domain — the /help command.

Lists built-in commands (from the assembled BUILTINS registry) plus
user-invocable skills. BUILTINS is imported lazily at reply time: this
module is one of the packages registry assembles from, so a module-level
import would be circular.
"""

from __future__ import annotations

from ..lang import t
from ..skills.loader import SkillLoader
from ._base import BuiltinCommand


def _help_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Registry is assembled from this package's siblings — a module-level
    # import would be circular, so bind BUILTINS at reply time.
    from .registry import BUILTINS

    # Built-in replies are ephemeral user-facing notices — inline bilingual
    # t() at source (i18n 分流规则: long-form command reports are the
    # copy-as-code track, not catalog keys).
    lines = [t("**Available commands**", "**可用命令**"), "", t("Built-in commands:", "内置命令：")]
    for name in sorted(BUILTINS):
        c = BUILTINS[name]
        lines.append(f"- `/{name}` — {t(*c.description)}")
    skills = [
        s
        for s in SkillLoader(project_slug=project_slug).load()
        if s.trigger in ("user-invocable", "both")
    ]
    if skills:
        lines += ["", t("Skills (invoke with `/skill-name [prompt]`):", "技能（用 `/技能名 [提示]` 调用）：")]
        for s in sorted(skills, key=lambda x: x.name):
            desc = (s.description or "").strip()
            lines.append(f"- `/{s.name}`" + (f" — {desc}" if desc else ""))
    else:
        lines += [
            "",
            t(
                "(No skills available yet — add them in Settings → Skills)",
                "（还没有可用技能 — 在 设置 → 技能 中添加）",
            ),
        ]
    return "\n".join(lines)


# 本域注册块：registry.py 装配 BUILTINS 时收集（命令清单契约快照钉住）。
COMMANDS: dict[str, BuiltinCommand] = {
    "help": BuiltinCommand(
        name="help",
        description=(
            "List available commands and skills",
            "列出可用命令与技能",
        ),
        handler=_help_handler,
    ),
}
