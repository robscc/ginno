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

from ..goals import store as goal_store
from ..goals.events import notify_goal_changed
from ..skills.loader import SkillLoader


@dataclass(frozen=True)
class BuiltinCommand:
    name: str
    description: str
    # (project_slug, session, args) -> reply text. session/args may be None
    # for commands that need no session context.
    handler: Callable[..., str]


def _help_handler(project_slug: str | None = None, session=None, args=None) -> str:
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


_GOAL_USAGE = (
    "**Goal 用法**\n"
    "- `/goal <objective>` — 为当前会话设定长程目标（Agent 自主多轮推进）\n"
    "- `/goal` — 查看当前目标\n"
    "- `/goal pause` / `/goal resume` — 暂停 / 恢复\n"
    "- `/goal clear` — 清除目标\n"
    "- `/goal edit` — 提示从顶部目标卡片编辑"
)

_STATUS_LABELS = {
    "active": "推进中",
    "paused": "已暂停",
    "blocked": "受阻",
    "usage_limited": "用量受限",
    "complete": "已达成",
}


def _goal_summary(goal: dict) -> str:
    label = _STATUS_LABELS.get(goal.get("status", ""), goal.get("status", ""))
    mins = int(goal.get("time_used_seconds", 0)) // 60
    turns = goal.get("turns_used", 0)
    return (
        f"**当前目标**（{label}）\n"
        f"- 目标：{goal.get('objective', '')}\n"
        f"- 进度：自主推进 {turns} 轮 · 已用 {mins} 分钟\n"
        f"- 命令：/goal pause · /goal resume · /goal clear · /goal edit"
    )


def _goal_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _GOAL_USAGE
    slug = session.get("project_slug") or "default"
    sid = session.get("session_id") or ""
    args = (args or "").strip()

    goal = goal_store.get_goal(slug, sid)

    if not args:
        return _goal_summary(goal) if goal else "（未设定目标）\n" + _GOAL_USAGE

    low = args.lower()
    if low == "pause":
        if not goal:
            return "（未设定目标）"
        if goal["status"] != goal_store.STATUS_ACTIVE:
            return f"目标当前为「{_STATUS_LABELS.get(goal['status'])}」，无需暂停"
        g = goal_store.update_status(slug, sid, goal_store.STATUS_PAUSED)
        notify_goal_changed(slug, sid, g)
        return "🎯 目标已暂停（/goal resume 恢复）"
    if low == "resume":
        if not goal:
            return "（未设定目标）"
        if goal["status"] == goal_store.STATUS_COMPLETE:
            return "目标已完成，不能恢复；用 `/goal <新目标>` 设定新目标"
        if goal["status"] == goal_store.STATUS_ACTIVE:
            return "目标正在推进中"
        g = goal_store.update_status(slug, sid, goal_store.STATUS_ACTIVE)
        notify_goal_changed(slug, sid, g)
        return "🎯 目标已恢复推进"
    if low == "clear":
        if not goal:
            return "（未设定目标）"
        goal_store.clear_goal(slug, sid)
        notify_goal_changed(slug, sid, None)
        return "目标已清除"
    if low == "edit":
        return "请点击聊天区顶部的目标卡片进行编辑（P0 暂不支持命令行直接编辑）"

    # `/goal <objective>` — set or replace
    if goal_store.is_open(goal):
        return (
            "当前已有未完成目标：\n" + _goal_summary(goal)
            + "\n\n如需替换：请先 `/goal clear`，或在顶部目标卡片中设定新目标（有确认提示）。"
        )
    try:
        g = goal_store.create_goal(slug, sid, args, agent_id=session.get("agent_id"))
    except ValueError as e:
        return f"设定失败：{e}"
    notify_goal_changed(slug, sid, g)
    return f"🎯 目标已设定，开始自主推进：\n{g['objective']}\n（/goal pause 可暂停）"


BUILTINS: dict[str, BuiltinCommand] = {
    "help": BuiltinCommand(
        name="help",
        description="列出可用命令与技能",
        handler=_help_handler,
    ),
    "goal": BuiltinCommand(
        name="goal",
        description="设定或查看本会话的长程目标（自主推进）",
        handler=_goal_handler,
    ),
}
