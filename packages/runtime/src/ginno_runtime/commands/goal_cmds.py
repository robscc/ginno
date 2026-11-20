"""Goal domain — /goal and /plan.

/goal manages the session long-running goal (set/show/pause/resume/clear);
/plan lists task plans and confirms/cancels pending ones (async — kicking
the plan watcher needs an event loop).
"""

from __future__ import annotations

from ..goals import store as goal_store
from ..goals.events import notify_goal_changed
from ..lang import t
from ._base import BuiltinCommand


_GOAL_USAGE_EN = (
    "**Goal usage**\n"
    "- `/goal <objective>` — set a long-running goal for this session (the agent advances autonomously)\n"
    "- `/goal` — show the current goal\n"
    "- `/goal pause` / `/goal resume` — pause / resume\n"
    "- `/goal clear` — clear the goal\n"
    "- `/goal edit` — points to the goal chip at the top for editing"
)

_GOAL_USAGE_ZH = (
    "**/goal 用法**\n"
    "- `/goal <目标>` — 为本会话设定长程目标（agent 自主推进）\n"
    "- `/goal` — 查看当前目标\n"
    "- `/goal pause` / `/goal resume` — 暂停 / 恢复\n"
    "- `/goal clear` — 清除目标\n"
    "- `/goal edit` — 指向顶部目标芯片进行编辑"
)

def _goal_usage() -> str:
    return t(_GOAL_USAGE_EN, _GOAL_USAGE_ZH)

_STATUS_LABELS = {
    # status words are protocol values; the LABELS are user-facing copy.
    "active": ("In progress", "进行中"),
    "paused": ("Paused", "已暂停"),
    "blocked": ("Blocked", "受阻"),
    "usage_limited": ("Usage limited", "用量受限"),
    "complete": ("Achieved", "已达成"),
}

def _goal_status_label(status: str) -> str:
    pair = _STATUS_LABELS.get(status)
    return t(*pair) if pair else status

def _goal_summary(goal: dict) -> str:
    label = _goal_status_label(goal.get("status", ""))
    mins = int(goal.get("time_used_seconds", 0)) // 60
    turns = goal.get("turns_used", 0)
    return (
        f"{t('**Current goal**', '**当前目标**')} ({label})\n"
        f"{t('- Objective:', '- 目标：')} {goal.get('objective', '')}\n"
        f"{t('- Progress:', '- 进度：')} "
        + t(
            f"{turns} autonomous turn(s) · {mins} min used",
            f"{turns} 个自主回合 · 已用 {mins} 分钟",
        )
        + "\n"
        + t(
            "- Commands: /goal pause · /goal resume · /goal clear · /goal edit",
            "- 命令：/goal pause · /goal resume · /goal clear · /goal edit",
        )
    )

def _no_goal() -> str:
    return t("(No goal set)", "（未设定目标）")

def _goal_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _goal_usage()
    slug = session.get("project_slug") or "default"
    sid = session.get("session_id") or ""
    args = (args or "").strip()

    goal = goal_store.get_goal(slug, sid)

    if not args:
        return _goal_summary(goal) if goal else _no_goal() + "\n" + _goal_usage()

    low = args.lower()
    if low == "pause":
        if not goal:
            return _no_goal()
        if goal["status"] != goal_store.STATUS_ACTIVE:
            return t(
                f"The goal is currently \"{_goal_status_label(goal['status'])}\" — nothing to pause",
                f"目标当前为「{_goal_status_label(goal['status'])}」——无需暂停",
            )
        g = goal_store.update_status(slug, sid, goal_store.STATUS_PAUSED)
        notify_goal_changed(slug, sid, g)
        return t("🎯 Goal paused (/goal resume to continue)", "🎯 目标已暂停（/goal resume 继续）")
    if low == "resume":
        if not goal:
            return _no_goal()
        if goal["status"] == goal_store.STATUS_COMPLETE:
            return t(
                "The goal is already achieved and cannot resume; set a new one with `/goal <objective>`",
                "目标已达成，无法恢复；请用 `/goal <目标>` 设定新目标",
            )
        if goal["status"] == goal_store.STATUS_ACTIVE:
            return t("The goal is already in progress", "目标已在进行中")
        g = goal_store.update_status(slug, sid, goal_store.STATUS_ACTIVE)
        notify_goal_changed(slug, sid, g)
        return t("🎯 Goal resumed", "🎯 目标已恢复")
    if low == "clear":
        if not goal:
            return _no_goal()
        goal_store.clear_goal(slug, sid)
        notify_goal_changed(slug, sid, None)
        return t("Goal cleared", "目标已清除")
    if low == "edit":
        return t(
            "Click the goal chip at the top of the chat to edit it (command-line editing not supported yet)",
            "点击聊天顶部的目标芯片进行编辑（暂不支持命令行编辑）",
        )

    # `/goal <objective>` — set or replace
    if goal_store.is_open(goal):
        return (
            t("An open goal already exists:", "已有进行中的目标：")
            + "\n"
            + _goal_summary(goal)
            + "\n\n"
            + t(
                "To replace it: run `/goal clear` first, or set a new goal from the goal chip (it asks for confirmation).",
                "如需替换：先运行 `/goal clear`，或通过目标芯片设置新目标（会请求确认）。",
            )
        )
    try:
        g = goal_store.create_goal(slug, sid, args, agent_id=session.get("agent_id"))
    except ValueError as e:
        return t(f"Failed to set goal: {e}", f"设置目标失败：{e}")
    notify_goal_changed(slug, sid, g)
    return (
        t("🎯 Goal set — advancing autonomously:", "🎯 目标已设定 —— 开始自主推进：")
        + f"\n{g['objective']}\n"
        + t("(/goal pause to pause)", "（/goal pause 暂停）")
    )

def _fmt_plan_line(plan: dict) -> str:
    items = plan.get("items") or []
    done = sum(1 for i in items if i.get("status") == "done")
    failed = sum(1 for i in items if i.get("status") in ("failed", "stopped"))
    detail = ", ".join(
        "{}: {}{}".format(
            i.get("item_id"),
            i.get("status"),
            f" — {(i.get('error') or '')[:60]}" if i.get("status") == "failed" else "",
        )
        for i in items
    )
    return (
        f"**{plan.get('plan_id')}** “{plan.get('title')}” — {plan.get('status')}, "
        f"{done}/{len(items)}{f', {failed} failed' if failed else ''}\n"
        f"  {detail}"
    )

def _plan_handler(project_slug: str | None = None, session=None, args=None) -> str:
    """Sync fallback（无事件循环的调用面）：只读列表——confirm/cancel 必须走
    async 路径（要踢 watcher）。"""
    if not session:
        return t("Usage: `/plan`", "用法：`/plan`")
    plans = []
    try:
        from ..plans import store as plan_store

        plans = plan_store.list_plans(
            project_slug or session.get("project_slug") or "default",
            session.get("session_id", ""),
        )
    except Exception:  # noqa: BLE001 — 命令面永不抛
        pass
    if not plans:
        return t("No plans in this session.", "本会话没有任务清单。")
    return "\n\n".join(_fmt_plan_line(p) for p in plans)

async def _plan_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/plan — 用户面清单操作（plan-orchestration §1：cancel/确认用户命令可达）。

    /plan 列出本会话清单；/plan confirm <id> 确认挂起清单；/plan cancel <id>
    取消清单。async 是因为 confirm/cancel 要踢 watcher（需要事件循环）。"""
    if not session:
        return t(
            "Usage: `/plan` (list) · `/plan confirm <id>` · `/plan cancel <id>`",
            "用法：`/plan`（列表）· `/plan confirm <id>`（确认）· `/plan cancel <id>`（取消）",
        )
    sid = session.get("session_id", "")
    slug = project_slug or session.get("project_slug") or "default"
    from ..plans import store as plan_store
    from ..plans import watcher as plan_watcher

    argv = (args or "").split()
    cmd = argv[0] if argv else ""
    pid = argv[1] if len(argv) > 1 else ""
    plans = plan_store.list_plans(slug, sid)
    if not plans:
        return t("No plans in this session.", "本会话没有任务清单。")
    if cmd in ("confirm", "cancel") and not pid:
        return "\n\n".join(
            [t(f"Missing plan id for /plan {cmd}.", f"/plan {cmd} 缺少清单 id。")]
            + [_fmt_plan_line(p) for p in plans]
        )
    if cmd == "confirm":
        updated = plan_store.confirm_plan(slug, sid, pid)
        if updated is None:
            return t(
                f"Plan {pid} not found or not awaiting confirmation.",
                f"清单 {pid} 不存在或不在等待确认状态。",
            )
        plan_watcher.notify_change(sid)
        return t(
            f"Plan {pid} confirmed — scheduling started.",
            f"清单 {pid} 已确认——开始调度。",
        )
    if cmd == "cancel":
        updated = plan_store.cancel_plan(slug, sid, pid)
        if updated is None:
            return t(f"Plan {pid} not found or already terminal.", f"清单 {pid} 不存在或已是终态。")
        plan_watcher.notify_change(sid)
        return t(f"Plan {pid} cancelled.", f"清单 {pid} 已取消。")
    return "\n\n".join([_fmt_plan_line(p) for p in plans])


# 本域注册块：registry.py 装配 BUILTINS 时收集（命令清单契约快照钉住）。
COMMANDS: dict[str, BuiltinCommand] = {
    "goal": BuiltinCommand(
        name="goal",
        description=(
            "Set or show a long-running goal for this session (advances autonomously)",
            "设定或查看本会话的长程目标（自主推进）",
        ),
        handler=_goal_handler,
    ),
    "plan": BuiltinCommand(
        name="plan",
        description=(
            "Task plans (/plan list; /plan confirm|cancel <id>)",
            "任务清单（/plan 列出；/plan confirm|cancel <id> 确认/取消）",
        ),
        handler=_plan_handler,
        async_handler=_plan_async_handler,
    ),
}
