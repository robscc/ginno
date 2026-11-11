"""Built-in slash command registry.

Built-ins short-circuit the turn: the handler renders a reply directly and the
graph never runs (no LLM call, no checkpoint write). ``/clear`` is the one
exception handler-side: its async path rewrites the checkpoint (empties the
messages channel) before replying.

Skills are NOT registered here — they live on disk (SKILL.md) and are resolved
by the resolver via ``SkillLoader``. Names collide → built-in wins (documented
in docs/commands-and-mentions-design.md).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import RemoveMessage

from ..goals import store as goal_store
from ..goals.events import notify_goal_changed
from ..lang import t
from ..skills.loader import SkillLoader

_log = logging.getLogger("ginno.commands")


@dataclass(frozen=True)
class BuiltinCommand:
    name: str
    # Bilingual (en, zh) — rendered through t() at reply time (locale is
    # request-scoped; module import time would freeze the wrong locale).
    description: tuple[str, str]
    # (project_slug, session, args) -> reply text. session/args may be None
    # for commands that need no session context.
    handler: Callable[..., str]
    # Optional async variant for commands that do real server work (LLM
    # calls, state rewrites). When set, the resolver routes the command to
    # the async path (TurnPlan.builtin_async) and the WS invoke handler
    # awaits it — busy-gated, because unlike a pure sync reply it can touch
    # thread state that must never race a live turn.
    async_handler: Callable[..., Awaitable[str]] | None = None
    # Channel-side (WeCom/DingTalk) visibility policy (docs/design/
    # channel-devswap-unattended-design.md §2):
    #   "all"   — anyone in the bound chat may trigger (default, compat);
    #   "admin" — only userids in the channel's admin_users config;
    #   "none"  — channel cannot trigger at all (desktop-only, reserved).
    # Enforced in channels/base.run_channel_turn; desktop WS never checks.
    channel_policy: str = "all"


def _help_handler(project_slug: str | None = None, session=None, args=None) -> str:
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


def _mounts_list(session) -> list[dict]:
    """Current mounts of a live session (resolved context-dir dicts)."""
    return list((session or {}).get("context_dirs") or [])


def _fmt_mounts(session) -> str:
    from .. import context_folders as cf

    dirs = _mounts_list(session)
    primary_path = (session or {}).get("primary_path") or ""
    if not dirs:
        return t(
            "(No context folders mounted in this session)",
            "（本会话未挂载上下文目录）",
        )
    lines = [t("**Context folders mounted in this session**", "**本会话挂载的上下文目录**"), ""]
    for d in dirs:
        if d.get("missing"):
            lines.append(
                f"- ❓ {d.get('id')} "
                + t("(folder missing, inactive)", "（目录缺失，未启用）")
            )
            continue
        tag = "rw" if d.get("access") == "rw" else t("ro (read-only)", "ro（只读）")
        star = (
            t(" ★primary", " ★主目录")
            if primary_path and d.get("path") == primary_path
            else ""
        )
        rf = cf.rule_file_for(d["path"])
        rule = f" ({rf.name})" if rf else ""
        lines.append(f"- **{d.get('name')}** `{d.get('path')}` [{tag}{star}]{rule}")
    return "\n".join(lines)


def _match_mount(dirs: list[dict], token: str) -> dict | None:
    tok = (token or "").strip().rstrip("/")
    if not tok:
        return None
    for d in dirs:  # exact name / exact path first
        if not d.get("missing") and tok in (d.get("name"), d.get("path")):
            return d
    from pathlib import Path as _P

    for d in dirs:  # then basename of the path
        if not d.get("missing") and _P(d.get("path") or "").name == tok:
            return d
    return None


_MOUNT_USAGE_EN = (
    "**Context folder usage**\n"
    "- `/mount <path> [ro|rw]` — mount a folder (registered in the folder library; rw by default)\n"
    "- `/mount` / `/mounts` — show current mounts\n"
    "- `/umount <name|path>` — unmount\n"
    "- `/primary <name|path>` — set the primary working directory (bash cwd)\n"
    "- `/primary clear` — clear the primary working directory\n"
    "The folder library and access tiers can also be managed in Settings → Context Folders."
)
_MOUNT_USAGE_ZH = (
    "**上下文目录用法**\n"
    "- `/mount <路径> [ro|rw]` — 挂载目录（登记进目录库；默认 rw）\n"
    "- `/mount` / `/mounts` — 查看当前挂载\n"
    "- `/umount <名称|路径>` — 卸载\n"
    "- `/primary <名称|路径>` — 设置主工作目录（bash cwd）\n"
    "- `/primary clear` — 清除主工作目录\n"
    "目录库与访问级别也可在 设置 → 上下文目录 中管理。"
)


def _mount_usage() -> str:
    return t(_MOUNT_USAGE_EN, _MOUNT_USAGE_ZH)


def _mount_handler(project_slug: str | None = None, session=None, args=None) -> str:
    from .. import context_folders as cf

    if not session:
        return _mount_usage()
    args = (args or "").strip()
    if not args:
        return _fmt_mounts(session) + "\n\n" + _mount_usage()

    # `<path> [ro|rw]` — path may contain spaces; access token is the tail.
    parts = args.split()
    access = cf.DEFAULT_ACCESS
    if len(parts) >= 2 and parts[-1].lower() in cf.ACCESS_TIERS:
        access = parts[-1].lower()
        path = " ".join(parts[:-1])
    else:
        path = args

    probe = cf.probe(path)
    if not probe.get("ok"):
        return t(f"Mount failed: {probe.get('error')}", f"挂载失败：{probe.get('error')}")
    folder = cf.add_folder(path, access=access, load_rules=True)

    dirs = _mounts_list(session)
    ids = [d["id"] for d in dirs if not d.get("missing")]
    if folder["id"] in ids:
        return (
            t(
                f"Folder already mounted: **{folder['name']}** `{folder['path']}`",
                f"目录已挂载：**{folder['name']}** `{folder['path']}`",
            )
            + "\n\n"
            + _fmt_mounts(session)
        )
    ids.append(folder["id"])
    # First mount becomes primary automatically (bash cwd switches to it);
    # later mounts never steal an existing primary.
    primary = session.get("primary_folder") or (folder["id"] if not dirs else None)

    from ..api.sessions import apply_session_context  # lazy: avoid import cycle

    res = apply_session_context(session.get("session_id", ""), ids, primary)
    if not res.get("ok"):
        return t(f"Mount failed: {res.get('error')}", f"挂载失败：{res.get('error')}")
    star = (
        t(", set as primary working directory (bash cwd)", "，并设为主工作目录（bash cwd）")
        if primary == folder["id"] and not dirs
        else ""
    )
    rf = probe.get("rule_file")
    rule_note = (
        t(
            f"; detected {rf}, its rules will be injected into context",
            f"；检测到 {rf}，其规则将注入上下文",
        )
        if rf
        else ""
    )
    return (
        t("✅ Mounted", "✅ 已挂载")
        + f" **{folder['name']}** `{folder['path']}`"
        + f" ({folder['access']}{star}){rule_note}\n\n"
        + _fmt_mounts(session)
    )


def _mounts_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _mount_usage()
    return _fmt_mounts(session)


def _umount_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _mount_usage()
    token = (args or "").strip()
    if not token:
        return (
            t("Usage: `/umount <name|path>`", "用法：`/umount <名称|路径>`")
            + "\n\n"
            + _fmt_mounts(session)
        )
    dirs = _mounts_list(session)
    hit = _match_mount(dirs, token)
    if not hit:
        return (
            t(f"Mount not found: {token}", f"未找到挂载：{token}")
            + "\n\n"
            + _fmt_mounts(session)
        )
    ids = [d["id"] for d in dirs if not d.get("missing") and d["id"] != hit["id"]]
    primary = session.get("primary_folder")
    if primary == hit["id"]:
        primary = None
    from ..api.sessions import apply_session_context  # lazy: avoid import cycle

    res = apply_session_context(session.get("session_id", ""), ids, primary)
    if not res.get("ok"):
        return t(f"Unmount failed: {res.get('error')}", f"卸载失败：{res.get('error')}")
    return (
        t("Unmounted", "已卸载")
        + f" **{hit.get('name')}** `{hit.get('path')}`\n\n"
        + _fmt_mounts(session)
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


def _primary_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _mount_usage()
    token = (args or "").strip()
    dirs = _mounts_list(session)
    if not token:
        return (
            t(
                "Usage: `/primary <name|path>` or `/primary clear`",
                "用法：`/primary <名称|路径>` 或 `/primary clear`",
            )
            + "\n\n"
            + _fmt_mounts(session)
        )
    ids = [d["id"] for d in dirs if not d.get("missing")]
    if token.lower() == "clear":
        primary = None
    else:
        hit = _match_mount(dirs, token)
        if not hit:
            return t(
                f"Mount not found: {token} (mount it with /mount first)",
                f"未找到挂载：{token}（请先用 /mount 挂载）",
            )
        primary = hit["id"]
    from ..api.sessions import apply_session_context  # lazy: avoid import cycle

    res = apply_session_context(session.get("session_id", ""), ids, primary)
    if not res.get("ok"):
        return t(f"Failed to set: {res.get('error')}", f"设置失败：{res.get('error')}")
    if primary is None:
        return (
            t(
                "Primary working directory cleared (cwd back to the session files dir)",
                "主工作目录已清除（cwd 回到会话文件目录）",
            )
            + "\n\n"
            + _fmt_mounts(session)
        )
    pp = res.get("primary_path") or ""
    return (
        t(
            f"Primary working directory set to `{pp}` (bash cwd and relative-path base)",
            f"主工作目录已设为 `{pp}`（bash cwd 与相对路径基准）",
        )
        + "\n\n"
        + _fmt_mounts(session)
    )


def _compact_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Never reached through the resolver (async_handler routes it); kept so
    # the registry entry is well-formed and direct sync calls get a hint.
    return t(
        "Run /compact through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /compact",
    )


async def _compact_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/compact — force the E3 history compaction NOW (manual trigger).

    Same machinery as the automatic turn-entry compaction (microcompact the
    stale tool outputs first, then LLM-summarize the old prefix), but with
    ``force=True``: no token threshold, works even when auto compaction is
    off. Long sessions run at 100k+ tokens are exactly where provider streams
    get fragile (2026-09-29 incident), so users need an on-demand lever.
    """
    if not session:
        return t("(/compact must be used inside a session)", "（/compact 必须在会话内使用）")
    sid = session.get("session_id") or ""
    graph = session.get("graph")
    if graph is None or not sid:
        return t("Session is not ready yet; try again shortly", "会话尚未就绪；请稍后再试")
    slug = session.get("project_slug") or project_slug or "default"
    config = {"configurable": {"thread_id": sid, "project_slug": slug}}

    from ..compaction import maybe_compact_history
    from ..microcompact import maybe_microcompact_history
    from ..tokens import estimate_messages_tokens

    try:
        snap = await graph.aget_state(config)
    except Exception:
        snap = None
    msgs = list((getattr(snap, "values", None) or {}).get("messages") or [])
    if not msgs:
        return t(
            "This session has no history messages to compact yet",
            "本会话还没有可压缩的历史消息",
        )
    if getattr(snap, "next", None):
        # `next` 非空有两种含义：本进程里有活的 turn（流式进行中，或停在
        # 权限/提问 interrupt——两者都在内存注册表里），此时拒绝压缩；或是
        # 进程重启/崩溃把 checkpoint 留在半空中（内存注册表已随旧进程消失，
        # 没有任何东西会再跑），此时应当修复（heal 清掉 next、补齐悬空
        # tool_calls）后照常压缩，而不是永远拒绝（2026-10-12：重启杀掉进行
        # 中 turn 后 /compact 一直报「停在确认步骤」）。
        # turn_gate_busy（2026-10-31）：三查同义 + 泄漏自愈（armed 超
        # RUNNING_TURN_STALE_S 且无活 task 的残留条目不再永久卡死命令）。
        from .. import server_shared as _shared

        if _shared.turn_gate_busy(sid):
            return t(
                "The current turn is running or paused at a confirmation step (permission / question); finish or stop it before running /compact",
                "当前回合仍在进行或停在确认步骤（权限 / 提问）上；请先完成或停止再运行 /compact",
            )
        try:
            from ..api.stream import _heal_interrupted_turn
            from ..server_shared import _turn_lock

            # 与 _stop_parked_turn 相同的串行化：防止 heal 期间另一个 tab 的
            # 新 invoke 与它交错写 checkpoint。heal 自身 best-effort。
            async with _turn_lock(sid):
                await _heal_interrupted_turn(graph, config)
        except Exception:
            # 绝不能在这里 `from ..server_shared import _log`：函数体内任何对
            # `_log` 的赋值/import 都会让它变成整个函数的局部名，而该行只在
            # heal 失败时执行——heal 成功后下面既有的 `_log.exception(...)`
            # 就会 UnboundLocalError（2026-10-12 实测炸过）。用模块级 `_log`。
            _log.exception("compact_cmd_zombie_heal_failed session=%s", sid)
    before_tokens = estimate_messages_tokens(msgs)

    notes: list[str] = []
    # Rung below E3: clear stale tool outputs first (pure state rewrite, no
    # LLM). Frees the most bytes per fidelity lost, same order as turn entry.
    try:
        micro = await maybe_microcompact_history(session, config)
        if micro:
            notes.append(
                t(
                    f"cleared {micro.get('cleared_tool_outputs', 0)} stale tool output(s)"
                    f" (~{micro.get('chars_freed', 0)} chars freed)",
                    f"清理了 {micro.get('cleared_tool_outputs', 0)} 条过期工具输出"
                    f"（约释放 {micro.get('chars_freed', 0)} 字符）",
                )
            )
    except Exception:
        _log.exception("compact_cmd_microcompact_failed session=%s", sid)

    try:
        # 「压缩中…」状态事件（与 turn 入口的自动压缩同一条 UI 契约）：
        # 只在真正决定压缩（过完所有早退检查）后、总结 LLM 调用前发出。
        from ..server_shared import _push_global_event, _push_session_event

        async def _announce_compacting() -> None:
            await _push_session_event(sid, "context.compacting", {})

        stats = await maybe_compact_history(
            session, config, force=True, on_summary_start=_announce_compacting
        )
    except Exception as e:
        _log.exception("compact_cmd_failed session=%s", sid)
        return t(
            f"Compaction failed: {type(e).__name__}: {e}",
            f"压缩失败：{type(e).__name__}: {e}",
        )

    if not stats:
        # No user-turn boundary to split at (history shorter than
        # compact_keep_turns) or the summarizer returned nothing.
        tail = "; ".join(notes)
        msg = t(
            "History not long enough: full turns haven't exceeded the retention threshold (compact_keep_turns); no old messages to summarize yet",
            "历史还不够长：完整回合数未超过保留阈值（compact_keep_turns）；暂无可摘要的旧消息",
        )
        return f"{msg} ({tail})" if tail else msg

    try:
        snap2 = await graph.aget_state(config)
        after_tokens = estimate_messages_tokens(
            list((getattr(snap2, "values", None) or {}).get("messages") or [])
        )
    except Exception:
        after_tokens = None

    lines = [
        t("✅ Context compacted", "✅ 上下文已压缩"),
        t(
            f"- LLM-summarized {stats['compacted_messages']} old message(s)"
            f" (summary of {stats['summary_chars']} chars), kept the most recent {stats['kept_messages']}",
            f"- LLM 摘要了 {stats['compacted_messages']} 条旧消息"
            f"（摘要 {stats['summary_chars']} 字符），保留最近 {stats['kept_messages']} 条",
        ),
    ]
    if after_tokens is not None:
        lines.append(
            t(
                f"- History tokens ~{before_tokens} → ~{after_tokens}",
                f"- 历史 tokens 约 {before_tokens} → 约 {after_tokens}",
            )
        )
    lines.extend(f"- {n}" for n in notes)
    return "\n".join(lines)


def _clear_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Never reached through the resolver (async_handler routes it); kept so
    # the registry entry is well-formed and direct sync calls get a hint.
    return t(
        "Run /clear through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /clear",
    )


async def _clear_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/clear — wipe this session's conversation history, start fresh.

    Same session_id keeps its identity: files dir, mounts, goal, settings all
    survive; only the checkpoint's ``messages`` channel is emptied (the delete-
    rewrite pattern of microcompact.py, minus the re-add). Busy-gated like
    /compact — a history rewrite must never race a live turn. After the
    rewrite, ``history.cleared`` is broadcast so every open UI drops its local
    transcript (the /history endpoint now rebuilds from an empty checkpoint);
    the receipt notice that follows lands as the first bubble of the fresh chat.
    """
    if not session:
        return t("(/clear must be used inside a session)", "（/clear 必须在会话内使用）")
    sid = session.get("session_id") or ""
    graph = session.get("graph")
    if graph is None or not sid:
        return t("Session is not ready yet; try again shortly", "会话尚未就绪；请稍后再试")
    slug = session.get("project_slug") or project_slug or "default"
    config = {"configurable": {"thread_id": sid, "project_slug": slug}}

    from ..tokens import estimate_messages_tokens

    try:
        snap = await graph.aget_state(config)
    except Exception:
        snap = None
    msgs = list((getattr(snap, "values", None) or {}).get("messages") or [])
    if not msgs:
        return t("This session's context is already empty", "本会话上下文已经是空的")
    if getattr(snap, "next", None):
        # `next` 非空与 /compact 同理有两种含义（详见 /compact 处注释）：本进程
        # 里有活的 turn（停在权限/提问 interrupt，UI 有卡可确认）→ 拒绝；进程
        # 重启/崩溃留下的僵尸 checkpoint（内存注册表已随旧进程消失，UI 没有
        # 任何可确认的地方，也没有东西会再跑）→ heal 后照常清空，而不是永远
        # 拒绝（2026-10-15 用户报告：/clear 报「停在确认步骤」但无卡可确认）。
        # turn_gate_busy（2026-10-31）：三查同义 + 泄漏自愈（armed 超
        # RUNNING_TURN_STALE_S 且无活 task 的残留条目不再永久卡死命令）。
        from .. import server_shared as _shared

        if _shared.turn_gate_busy(sid):
            return t(
                "The current turn is paused at a confirmation step (permission / question); finish it before running /clear",
                "当前回合停在确认步骤（权限 / 提问）上；请先完成再运行 /clear",
            )
        try:
            from ..api.stream import _heal_interrupted_turn
            from ..server_shared import _turn_lock

            # 与 _stop_parked_turn 相同的串行化（防止 heal 与新 invoke 交错写
            # checkpoint）；heal 自身 best-effort，失败也继续清空。
            async with _turn_lock(sid):
                await _heal_interrupted_turn(graph, config)
        except Exception:
            _log.exception("clear_cmd_zombie_heal_failed session=%s", sid)
    if any(not getattr(m, "id", None) for m in msgs):
        # id-less messages can't be delete-rewritten (microcompact bails the
        # same way); nothing re-added means they'd survive as ghosts.
        return t(
            "Some history messages carry no id and cannot be removed safely; run /compact instead",
            "部分历史消息缺少 id，无法安全清除；请改用 /compact",
        )

    freed = estimate_messages_tokens(msgs)
    await graph.aupdate_state(
        config,
        {"messages": [RemoveMessage(id=m.id) for m in msgs]},
        as_node="agent",
    )
    from ..server_shared import _push_global_event, _push_session_event

    await _push_session_event(sid, "history.cleared", {})
    return (
        t(
            f"🧹 Context cleared — removed {len(msgs)} message(s) (~{freed} tokens freed).",
            f"🧹 上下文已清空 —— 移除了 {len(msgs)} 条消息（约释放 {freed} tokens）。",
        )
        + "\n"
        + t(
            "The conversation starts fresh; session files, mounts and settings are kept.",
            "对话从新开始；会话文件、挂载与设置均保留。",
        )
    )


def _fork_handler(project_slug: str | None = None, session=None, args=None) -> str:
    return t(
        "Run /fork through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /fork",
    )


async def fork_session_core(sid: str, slug: str, meta: dict, session: dict) -> dict:
    """/fork 与 POST /api/sessions/{id}/fork 共用的复制核心。

    返回 ``{"ok": bool, "error"?: str, "new_id"?: str, "copied"?: int,
    "existing"?: bool}``；调用方负责摊成自己的回复形态（IM 双语文本 /
    REST JSON）。busy-gate（带泄漏自愈，server_shared.turn_gate_busy）与
    一会话一副本幂等都在这里。
    """
    from .. import server_shared as shared

    if shared.turn_gate_busy(sid):
        return {"ok": False, "error": "busy"}

    # 一会话一副本：已有存活副本 → 幂等返回（防双胞胎）；副本被删 →
    # 稍后用新值覆盖死引用重新 fork（patch 丢 None，无需显式清键）。
    existing = meta.get("forked_to_session")
    if existing:
        from ..session_meta import _find_meta

        if _find_meta(str(existing)):
            return {"ok": True, "new_id": str(existing), "existing": True}

    from ..api.sessions import CreateSessionRequest, create_session

    channel = str(meta.get("channel") or "")
    is_channel = meta.get("type") == "channel"
    src_title = str(meta.get("title") or "").strip() or (
        {"dingtalk": "钉钉对话", "wecom": "企业微信对话"}.get(channel, "IM 对话")
        if is_channel else "会话"
    )
    # 副本标题带溯源后缀（2026-10-31 用户要求）：fork 出来的会话一眼可辨
    # 来源；侧栏超长自动截断，不担心占宽。
    fork_title = f"{src_title} (fork from {sid[:8]})"
    new_meta = await create_session(
        CreateSessionRequest(
            project_slug=slug,
            workspace="",
            agent_id=meta.get("agent_id") or session.get("agent_id") or None,
            title=fork_title,
            # 思考档位随副本(thinking-effort-design §8):源会话显式选过档位
            # 就带过去;auto 走 agent 默认链(provider/model 的 fork 语义不变,
            # 仍由 agent 绑定解析)。
            thinking_effort=str(meta.get("thinking_effort") or "") or None,
        )
    )
    if new_meta.get("ok") is False or not new_meta.get("id"):
        return {"ok": False, "error": "create_failed"}
    new_id = str(new_meta["id"])

    import time as _time

    stamp = _time.strftime("%m-%d %H:%M", _time.localtime())
    label = {"dingtalk": "钉钉", "wecom": "企业微信"}.get(channel, "IM")
    label_en = {"dingtalk": "DingTalk", "wecom": "WeCom"}.get(channel, "IM")
    brief = t(
        f"[turn context] Continued from a {label_en} conversation ({stamp}). "
        "Its current transcript follows — keep discussing the results here.",
        f"[turn context] 续自{label}对话（{stamp}）。以下为其当前记录，可就结果继续沟通。",
    ) if is_channel else t(
        f"[turn context] Continued from session {sid[:8]} ({stamp}). "
        "Its current transcript follows — keep discussing the results here.",
        f"[turn context] 续自会话 {sid[:8]}（{stamp}）。以下为其当前记录，可就结果继续沟通。",
    )
    try:
        from ..subagent_scheduler import _seed_fork_history

        copied = await _seed_fork_history(sid, new_id, slug, brief)
    except Exception:  # noqa: BLE001 — 种子失败要显式报错（副本没历史不能用）
        from ..server_shared import _log

        _log.exception("channel_fork_seed_failed src=%s child=%s", sid, new_id)
        return {"ok": False, "error": "seed_failed"}

    from ..session_meta import _session_meta_patch

    # 双向标记：新会话记来源（渠道名或源会话 id——溯源可辨）；源会话记
    # 副本落点（幂等键）。源会话 touch=False——复制不该把源会话顶到侧栏
    # 顶端。
    _session_meta_patch(
        slug, new_id,
        {"forked_from_channel": channel} if channel
        else {"forked_from_session": sid})
    _session_meta_patch(slug, sid, {"forked_to_session": new_id}, touch=False)

    from ..server_shared import _log

    _log.info(
        "channel_forked src=%s child=%s channel=%s messages=%d",
        sid, new_id, channel or "-", copied,
    )
    return {"ok": True, "new_id": new_id, "copied": copied}


async def _fork_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/fork — 把本会话复制为一个新的桌面普通会话（渠道或桌面均可）。

    范式 = ``api/schedule.py: fork_run``：``create_session`` +
    ``_seed_fork_history`` 复制 checkpoint 全量 messages（id 重铸，顶部
    一条 [turn context] 溯源行）。源会话本体不动。2026-10-31 起桌面会话
    也可 fork（侧栏扇形菜单入口）；渠道语义不变。

    语义要点（im-fork-to-desktop 设计 v0.4）：
    - ``/clear`` 边界天然尊重：clear 是对 checkpoint messages 的真删，
      而 checkpoint 正是唯一复制源——副本天然只带 clear 后的「眼前所见」。
    - 幂等（一会话一副本）：源 meta 记 ``forked_to_session``，副本存活时
      重复 /fork 返回既有副本；副本被删则重新 fork。
    - busy-gate（/clear 同口径，带泄漏自愈）：turn 在跑 / 停在确认步骤时
      拒绝，防止复制到写了一半的 checkpoint。
    - ``/fork <附带消息>`` 的「带话过去」留给 P1——附带内容暂被忽略并提示。
    """
    if not session or not (session.get("session_id") or ""):
        return t("(/fork must be used inside a session)", "（/fork 必须在会话内使用）")
    sid = session["session_id"]

    from ..session_meta import _find_meta

    found = _find_meta(sid)
    meta, slug = found if found else (None, str(project_slug or "default"))
    if not meta:
        return t(
            "/fork must be used inside a session with an index entry.",
            "/fork 需要在有索引记录的会话内使用。",
        )
    is_channel = meta.get("type") == "channel"
    channel = str(meta.get("channel") or "")
    label = {"dingtalk": "钉钉", "wecom": "企业微信"}.get(channel, "IM")

    extra = str(args or "").strip()
    extra_note = (
        t(
            " (The message attached to /fork is not carried over yet — that's a planned follow-up.)",
            "（/fork 后附带的消息暂不支持带走，属于后续版本功能。）",
        )
        if extra
        else ""
    )

    r = await fork_session_core(sid, slug, meta, session)
    if not r.get("ok"):
        err = r.get("error")
        if err == "busy":
            return t(
                "⏳ The AI is still replying (or a confirmation is pending) — "
                "send /fork again after it finishes.",
                "⏳ AI 正在回复（或停在确认步骤），结束后再发 /fork。",
            )
        if err == "seed_failed":
            return t(
                "Fork failed while copying history — please try again later.",
                "复制失败：历史搬运出错，请稍后再试。",
            )
        return t("Fork failed: could not create the desktop session.",
                 "复制失败：桌面会话创建失败。")
    new_id = str(r.get("new_id"))
    src_title = str(meta.get("title") or "").strip() or (
        f"{label}对话" if is_channel else "会话"
    )
    if r.get("existing"):
        return t(
            f'✅ Already copied — desktop session "{src_title}" '
            "(no second copy is made).",
            f"✅ 已复制过：桌面普通对话《{src_title}》，不会产生第二份。",
        )
    copied = int(r.get("copied") or 0)
    if is_channel:
        return t(
            f'✅ Copied to desktop session "{src_title}" ({copied} messages). '
            "Open Ginno on your desktop to continue — this conversation stays as is."
            + extra_note,
            f"✅ 已复制到桌面普通对话《{src_title}》（{copied} 条消息）。历史已带走，"
            "回电脑打开 Ginno 即可继续；本对话保持不变，在这里聊仍然照旧。" + extra_note,
        )
    return t(
        f'✅ Forked to new session "{src_title}" ({copied} messages); '
        "open it from the sidebar." + extra_note,
        f"✅ 已复制为新会话《{src_title}》（{copied} 条消息）；请在侧栏打开继续。"
        + extra_note,
    )


_SUBAGENT_USAGE_EN = (
    "**Subagent usage**\n"
    "- `/subagent <goal>` — create 1 subagent right away (constraints/acceptance "
    "left empty; refine later via steering in the sub-session)\n"
    "- `/subagent split <task description>` — decompose the task with the current "
    "model first, producing a plan card; spawns in bulk after you confirm\n"
    "- `/subagent-split <task description>` — same as the split form (alias)\n"
    "Splitting again before confirming overwrites the previous plan; each subagent's "
    "result is returned to this conversation automatically when it completes."
)
_SUBAGENT_USAGE_ZH = (
    "**/subagent 用法**\n"
    "- `/subagent <目标>` — 直接创建 1 个子代理（约束/验收留空；"
    "之后可在子会话中通过引导消息补充）\n"
    "- `/subagent split <任务描述>` — 先用当前模型拆解任务，生成方案卡片；"
    "确认后批量启动\n"
    "- `/subagent-split <任务描述>` — 同 split 形式（别名）\n"
    "确认前再次拆分会覆盖上一份方案；每个子代理完成后其结果会自动返回本对话。"
)


def _subagent_usage() -> str:
    return t(_SUBAGENT_USAGE_EN, _SUBAGENT_USAGE_ZH)


def _subagent_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Never reached through the resolver (async_handler routes /subagent); kept
    # so the registry entry is well-formed and direct sync calls get a hint.
    return t(
        "Run /subagent through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /subagent",
    )


def _split_form_task(args: str) -> str | None:
    """`/subagent split <task>` — the split keyword is the leading token
    (`split`, or the legacy Chinese `拆分`); the task description is
    everything after it (colon/space separators tolerated). None when the
    args are not the split form."""
    s = (args or "").lstrip()
    for kw in ("split", "拆分"):
        if s.startswith(kw):
            return s[len(kw):].lstrip(" ：:　")
    return None


def _is_split_form(args: str) -> bool:
    return _split_form_task(args) is not None


async def _subagent_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/subagent — direct form spawns ONE subagent immediately (origin=user);
    split form (拆分) runs the LLM decompose → subagent.plan card (P2 contract
    3). Busy-gated like /compact: the decompose LLM call must not race a live
    turn's supersteps, and the direct spawn path creates a session + graph."""
    if not session:
        return _subagent_usage()
    args = (args or "").strip()
    if not args:
        return _subagent_usage()
    if _is_split_form(args):
        task = _split_form_task(args) or ""
        if not task:
            return _subagent_usage()
        from ..subagent_plan import decompose_and_issue

        try:
            return await decompose_and_issue(session, task)
        except ValueError as e:
            return t(f"Split failed: {e}", f"拆分失败：{e}")
    from ..subagent_scheduler import create_subagent  # lazy: cycle

    res = await create_subagent(
        session.get("session_id") or "", args, "", "", origin="user"
    )
    if not res.get("ok"):
        return str(res.get("error") or "[error] spawn failed")
    return (
        t(
            f"✅ subagent started session_id={res['session_id']}"
            f" title={res['title']} depth={res['depth']}\n"
            "It runs independently in the background; its result is returned to this conversation when it completes.",
            f"✅ 子代理已启动 session_id={res['session_id']}"
            f" 标题={res['title']} 深度={res['depth']}\n"
            "它在后台独立运行；完成后其结果会自动返回本对话。",
        )
    )


async def _subagent_split_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/subagent-split <任务> — the split form under an explicit alias; the
    whole tail is the task description (no 拆分 keyword needed)."""
    if not session:
        return _subagent_usage()
    task = (args or "").strip()
    if not task:
        return _subagent_usage()
    from ..subagent_plan import decompose_and_issue

    try:
        return await decompose_and_issue(session, task)
    except ValueError as e:
        return t(f"Split failed: {e}", f"拆分失败：{e}")


def _think_usage() -> str:
    return t(
        "**Thinking effort usage**\n"
        "- `/think <off|low|medium|high>` — set this session's thinking effort\n"
        "- `/think auto` — follow the provider config (default)\n"
        "- `/think` — show the current effort",
        "**/think 用法**\n"
        "- `/think <off|low|medium|high>` — 设置本会话思考强度\n"
        "- `/think auto` — 跟随 provider 配置（缺省）\n"
        "- `/think` — 查看当前强度",
    )


def _think_handler(project_slug: str | None = None, session=None, args=None) -> str:
    """/think — 查询走同步路径;换档走 async(busy-gated,不与运行中 turn 抢)。"""
    if not session:
        return _think_usage()
    arg = (args or "").strip().lower()
    if arg not in ("", "auto", "off", "low", "medium", "high"):
        return _think_usage()
    if arg:
        return ""  # 动作交给 async_handler
    cur = str(session.get("thinking_effort") or "auto")
    return (
        t("Current thinking effort", "当前思考强度")
        + f": **{cur}**\n\n"
        + _think_usage()
    )


async def _think_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    if not session:
        return _think_usage()
    arg = (args or "").strip().lower()
    if arg not in ("auto", "off", "low", "medium", "high"):
        return _think_usage()
    sid = session.get("session_id") or session.get("id") or ""
    # 懒导入防环:api.sessions 经 ws/stream 链路最终引用本模块。
    from ..api.sessions import PatchSessionRequest, patch_session

    r = await patch_session(sid, PatchSessionRequest(thinking_effort=arg))
    if not r.get("ok"):
        return t(
            f"Thinking effort change failed: {r.get('error')}",
            f"思考强度切换失败：{r.get('error')}",
        )
    return (
        t("Thinking effort set to", "思考强度已设为")
        + f" **{arg}** — "
        + t("effective from the next model call.", "下一次模型调用起生效。")
    )





# /dev-swap 后台任务持有集：防 GC + 便于观测（完成后自动移出）。
_DEV_SWAP_TASKS: set[asyncio.Task] = set()

# 构建尾部输出里的 ANSI 转义（pytest/make/dev_swap.sh 的颜色码）——
# 原样塞进聊天 notice 会显示成 [31m 这种乱码，推送前剥掉。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


async def _dev_swap_background(sid: str, repo: str) -> None:
    """后台跑 make dev-swap（构建 3-6 分钟）；完成即广播 finish 信号。

    成功 → ``runtime.dev_swap_pending``（前端 invoke 壳 finish_dev_swap：
    重启+指纹验证+失败回滚，之后本进程被重启换掉——这就是闭环）。
    失败 → notice 带构建尾部输出（当前运行不受任何影响，未切换）。

    进度可视化（docs/dev-swap-progress-design.md §2.3）：逐行读 stdout，
    解析 dev_swap.sh 的 ``[devswap-progress] {json}`` 行直调
    ``api.devswap.update_progress`` 回填同一进度通道（与 shell 的 curl
    上报以签名去重）；notice 尾 600 字符逻辑不变。
    """
    import os as _os

    from ..server_shared import _push_global_event, _push_session_event

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "scripts/dev_swap.sh",
            cwd=repo,
            env={
                **_os.environ,
                "DEV_SWAP_REQUESTED_BY": "agent",
                # 发起会话随 env 传给 shell → state.json → 重启后新进程的
                # finish 终态上报（api/devswap.watch_swap_finish）——前端
                # §4.5 会话门控与结果落块都靠它。
                "DEV_SWAP_SESSION_ID": sid,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        await _push_session_event(
            sid, "notice", {"message": "/dev-swap: repo path invalid: " + repo}
        )
        return

    from ..api.devswap import ProgressReport, update_progress

    tail_buf: list[bytes] = []
    tail_len = 0
    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            tail_buf.append(line)
            tail_len += len(line)
            while tail_len > 4096 and len(tail_buf) > 1:  # 只留尾 4KB 供 notice
                tail_len -= len(tail_buf.pop(0))
            text = line.decode(errors="replace").strip()
            if not text.startswith("[devswap-progress]"):
                continue
            payload = text[len("[devswap-progress]"):].strip()
            try:
                import json as _json

                data = _json.loads(payload)
                if not data.get("run_id"):
                    continue
                update_progress(
                    ProgressReport(
                        run_id=str(data["run_id"]),
                        stage=str(data["stage"]),
                        state=str(data["state"]),
                        detail=data.get("detail"),
                        pct=data.get("pct"),
                        log_tail=data.get("log_tail"),
                        sid=sid or None,
                    )
                )
            except Exception:
                continue  # 进度行解析失败绝不影响构建结果
    except asyncio.CancelledError:
        from ..server_shared import _DEVSWAP_PROGRESS as _DEVSWAP_SNAPSHOT

        # 用户取消（2026-11-05：左上热更新按钮二击 → /api/devswap/cancel）。
        # kill 构建子进程树（start_new_session 语义，kill 整组），进度标
        # cancelled；未切 symlink 前取消零风险。
        try:
            _os.killpg(_os.getpgid(proc.pid), _os.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        update_progress(
            ProgressReport(
                run_id=str(
                    (_DEVSWAP_SNAPSHOT().get("snapshot") or {}).get("run_id") or "cancelled"
                ),
                stage="build_runtime",
                state="fail",
                detail="cancelled by user",
                sid=sid or None,
            )
        )
        raise
    await proc.wait()
    tail = _ANSI_RE.sub("", b"".join(tail_buf)[-600:].decode(errors="replace"))
    if proc.returncode == 0:
        # finish 阶段由 shell 已报 running；壳收尾终态由重启后的新进程接管。
        # 空 sid（侧栏热更新按钮等无会话发起方，2026-11-05）→ 全局广播，
        # 保证任意会话的前端都能收到确认门事件。
        if sid:
            await _push_session_event(
                sid,
                "runtime.dev_swap_pending",
                {"message": "dev-swap build ready; finishing swap (restart + verify)"},
            )
        else:
            await _push_global_event(
                "runtime.dev_swap_pending",
                {"message": "dev-swap build ready; finishing swap (restart + verify)"},
            )
    else:
        if sid:
            await _push_session_event(
                sid, "notice",
                {"message": f"/dev-swap failed (exit {proc.returncode}), nothing swapped:\n{tail}"},
            )
        else:
            await _push_global_event(
                "notice",
                {"message": f"dev-swap failed (exit {proc.returncode}), nothing swapped:\n{tail}"},
            )


async def _dev_swap_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/dev-swap — 开发热切换（docs/runtime-hot-swap-design.md）。

    产线闭环：本命令跑 build 段（quiesce 检查 → 单测 → make runtime →
    blue-green 预检 → symlink 原子切），随后广播 finish 信号由壳完成
    「重启 + 指纹验证 + 失败回滚」。**发起本命令的 turn 会在重启中被
    换掉**（发起者即旧进程）——回复只到「构建启动」，重启后由 TopBar
    dev 指纹黄条/回滚 notice 给出结局。

    repo 发现顺序：/dev-swap <repo路径> 显式参数 > build-info.repo
    （打包时写入的源码树路径）。
    """
    sid = (session or {}).get("session_id") or ""
    repo = (args or "").strip()
    if repo.startswith(("http://", "https://", "git@", "ssh://", "git://")):
        # 传 git URL 是最高频误用——单独给提示，别落到泛泛的 usage 文案。
        return t(
            "(/dev-swap needs a *local* ginno checkout — a git URL won't work. "
            "Clone it first (`git clone <url>`), then `/dev-swap /path/to/ginno`.)",
            "（/dev-swap 需要**本地**源码树——git URL 不行。先 `git clone <url>`，"
            "再 `/dev-swap /本地路径/ginno`。）",
        )
    if not repo:
        from ..world_state import context_settings

        repo = str(context_settings().get("dev_swap_repo") or "")
    if not repo:
        from .._build import build_info

        repo = str(build_info().get("repo") or "")
    from pathlib import Path as _P

    if not repo or not (_P(repo) / "scripts" / "dev_swap.sh").exists():
        return t(
            "(/dev-swap needs the ginno repo path — usage: /dev-swap /path/to/ginno)",
            "（/dev-swap 需要 ginno 源码树路径——用法：/dev-swap /path/to/ginno）",
        )
    task = asyncio.create_task(_dev_swap_background(sid, repo))
    # 命令路径也注册到进度快照的 task 位（2026-11-11 补齐：此前只有
    # /api/devswap/start 记 task，/dev-swap 命令发起的 run 对 cancel
    # 端点不可见——「取消热更新」对命令路径失效）。
    from ..server_shared import _DEVSWAP_PROGRESS as _DS_PROGRESS

    _DS_PROGRESS["task"] = task
    _DEV_SWAP_TASKS.add(task)
    task.add_done_callback(_DEV_SWAP_TASKS.discard)
    return t(
        "Dev-swap build started (~3-6 min: tests → PyInstaller → preflight → "
        "symlink swap). I'll restart onto the new bundle when it's ready.",
        "热切换构建已启动（约 3-6 分钟：单测 → PyInstaller → 预检 → symlink 切换）。"
        "构建完成后我会自动重启到新 bundle。",
    )


# --------------------------------------------------------------------------- #
# /workflow-{action} 命令族（docs/at-session-and-mention-scope-design.md §六）
#
# workflow 退出 @ 提及后的执行入口：7 条对 api/workflows.py 端点的薄封装
# （BUILTINS 注册模式，/fork 先例；连字符风格与 /dev-swap 一致）。只做
# 名字/参数解析 + 回执排版，业务语义全部留在端点函数里——slash 层零业务。
# ---------------------------------------------------------------------------

_WORKFLOW_FAMILY_USAGE_EN = (
    "**Workflow command family**\n"
    "- `/workflow-run <name|id>` — start a run (fuzzy name match; lists candidates "
    "when ambiguous)\n"
    "- `/workflow-status [run_id]` — status card of the latest run (or a given one)\n"
    "- `/workflow-pause [run_id]` — pause the latest (or given) running run\n"
    "- `/workflow-resume [run_id]` — resume a paused run\n"
    "- `/workflow-cancel [run_id]` — cancel the latest (or given) run\n"
    "- `/workflow-list` — list workflow definitions (name · version · steps · last run)\n"
    "- `/workflow-delete <name|id>` — delete a definition (run history is kept)"
)
_WORKFLOW_FAMILY_USAGE_ZH = (
    "**/workflow 命令族**\n"
    "- `/workflow-run <名称|id>` — 运行工作流（名称模糊匹配；多命中列出候选）\n"
    "- `/workflow-status [run_id]` — 最近一次 run（或指定 run）的状态卡\n"
    "- `/workflow-pause [run_id]` — 暂停最近（或指定）正在运行的 run\n"
    "- `/workflow-resume [run_id]` — 恢复已暂停的 run\n"
    "- `/workflow-cancel [run_id]` — 取消最近（或指定）run\n"
    "- `/workflow-list` — 工作流定义简表（名称 · 版本 · 步骤数 · 最近 run）\n"
    "- `/workflow-delete <名称|id>` — 删除定义（历史 run 保留）"
)


def _wf_family_usage() -> str:
    return t(_WORKFLOW_FAMILY_USAGE_EN, _WORKFLOW_FAMILY_USAGE_ZH)


def _wf_async_only_hint(cmd: str):
    def _hint(project_slug: str | None = None, session=None, args=None) -> str:
        return t(
            f"(/{cmd} runs asynchronously — just send it plain)",
            f"（/{cmd} 异步执行——直接发送即可）",
        )

    return _hint


async def _wf_call(coro):
    """Await an api/workflows endpoint coroutine; HTTPException → error str.

    Returns ``(ok, data-or-error)`` so handlers stay flat — the slash layer
    never lets a FastAPI exception escape into a turn reply."""
    from fastapi import HTTPException

    try:
        return True, await coro
    except HTTPException as e:
        return False, str(e.detail)


# status words are protocol values; the map supplies user-facing copy.
_WF_RUN_STATUS_LABELS = {
    "running": ("running", "运行中"),
    "paused": ("paused", "已暂停"),
    "done": ("done", "已完成"),
    "failed": ("failed", "已失败"),
    "cancelled": ("cancelled", "已取消"),
    "interrupted": ("interrupted", "已中断"),
}


def _wf_status_label(status: object) -> str:
    pair = _WF_RUN_STATUS_LABELS.get(str(status or ""))
    return t(*pair) if pair else str(status or "?")


def _wf_run_line(run: dict) -> str:
    name = run.get("name") or run.get("workflow_id") or "?"
    return f"- `{run.get('id')}` — {name} · {_wf_status_label(run.get('status'))}"


def _wf_match_defs(token: str) -> list[dict]:
    """name|id 匹配梯子：精确 id > 精确 name > name 子串（大小写不敏感）。"""
    from .. import workflows as wf_store

    token = (token or "").strip()
    defs = wf_store.list_defs()
    by_id = [d for d in defs if d.get("id") == token]
    if by_id:
        return by_id
    low = token.lower()
    exact = [d for d in defs if (d.get("name") or "").lower() == low]
    if exact:
        return exact
    return [d for d in defs if low in (d.get("name") or "").lower()]


def _wf_defs_table(defs: list[dict]) -> str:
    lines = []
    for d in defs:
        n = len(d.get("steps") or [])
        lines.append(
            f"- **{d.get('name')}** `{d.get('id')}` · "
            + t(f"v{d.get('version')} · {n} steps", f"v{d.get('version')} · {n} 步")
        )
    return "\n".join(lines)


async def _wf_resolve_run(args: str | None) -> tuple[dict | None, str]:
    """`[run_id]` 缺省解析（status/pause/resume/cancel 共用）。

    显式 id → 存在性校验；缺省 → 多个 running 时不猜、列候选让用户补参
    数，否则取全局最近 run（list_runs newest-first）。
    返回 ``(run, error_reply)``，二者互斥（run 为 None 时 error 非空）。"""
    from .. import workflows as wf_store

    token = (args or "").strip()
    if token:
        run = wf_store.get_run(token)
        if not run:
            return None, t(
                f"Run `{token}` not found — send `/workflow-status` to see the latest run.",
                f"未找到 run `{token}`——发送 `/workflow-status` 查看最近一次 run。",
            )
        return run, ""
    running = wf_store.list_runs(status="running")
    if len(running) > 1:
        lines = [
            t(
                "**Multiple runs are in progress — pass a run_id:**",
                "**有多个正在运行的 run——请带上 run_id 指定：**",
            ),
            "",
        ]
        lines += [_wf_run_line(r) for r in running[:10]]
        return None, "\n".join(lines)
    all_runs = wf_store.list_runs()
    if not all_runs:
        return None, t(
            "No workflow runs yet — start one with `/workflow-run <name|id>`.",
            "还没有任何工作流运行——用 `/workflow-run <名称|id>` 启动一次。",
        )
    return all_runs[0], ""


def _wf_no_defs_reply() -> str:
    return t(
        "No workflows yet — create one on the Workflows page (or synthesize "
        "one from a session), then run it here.",
        "还没有工作流——先在 Workflows 页创建（或从会话总结生成），再在这里运行。",
    )


async def _workflow_run_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    token = (args or "").strip()
    if not token:
        return _wf_family_usage()
    from .. import workflows as wf_store

    if not wf_store.list_defs():
        return _wf_no_defs_reply()
    cands = _wf_match_defs(token)
    if not cands:
        return (
            t(
                f"No workflow matches `{token}`. Existing workflows:",
                f"没有匹配 `{token}` 的工作流。现有：",
            )
            + "\n"
            + _wf_defs_table(wf_store.list_defs())
        )
    if len(cands) > 1:
        return (
            t(
                f"`{token}` matches {len(cands)} workflows — pass the exact name or id:",
                f"`{token}` 匹配到 {len(cands)} 个工作流——请用完整名称或 id 指定：",
            )
            + "\n"
            + _wf_defs_table(cands)
        )
    wf = cands[0]
    sid = (session or {}).get("session_id") or None
    ok, data = await _wf_call(
        wf_api.create_workflow_run_endpoint({"workflow_id": wf["id"], "session_id": sid})
    )
    if not ok:
        return t(f"❌ Failed to start the run: {data}", f"❌ 启动失败：{data}")
    run = (data or {}).get("run") or {}
    return t(
        f"✅ Workflow **{wf.get('name')}** started — run `{run.get('id')}` "
        f"(v{run.get('dsl_version')}).\n"
        "You'll be notified when it finishes; `/workflow-status` shows progress.",
        f"✅ 工作流 **{wf.get('name')}** 已启动 —— run `{run.get('id')}`"
        f"（v{run.get('dsl_version')}）。\n"
        "完成时会收到通知；`/workflow-status` 可查进度。",
    )


def _wf_status_card(run: dict) -> str:
    steps = run.get("steps") or []
    done = sum(1 for s in steps if s.get("status") in ("done", "skipped"))
    lines = [
        t(
            f"**Workflow run** {run.get('name') or run.get('workflow_id')} "
            f"`{run.get('id')}` — {_wf_status_label(run.get('status'))}",
            f"**工作流运行** {run.get('name') or run.get('workflow_id')} "
            f"`{run.get('id')}` —— {_wf_status_label(run.get('status'))}",
        ),
        t(f"Progress: {done}/{len(steps)} steps", f"进度：{done}/{len(steps)} 步"),
    ]
    pi = run.get("pending_interrupt")
    if isinstance(pi, dict) and pi.get("kind"):
        q = pi.get("question") or pi.get("node") or pi.get("kind")
        lines.append(
            t(
                f"⏸ Waiting for a human decision ({q}) — answer it on the "
                "Workflows page; a plain `/workflow-resume` cannot supply the answer.",
                f"⏸ 正在等待人工决定（{q}）——请到 Workflows 页回答；"
                "直接 `/workflow-resume` 无法代答。",
            )
        )
    if run.get("status") == "failed" and run.get("error"):
        lines.append(t(f"Error: {run['error']}", f"错误：{run['error']}"))
    return "\n".join(lines)


async def _workflow_status_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    run, err = await _wf_resolve_run(args)
    return err if run is None else _wf_status_card(run)


async def _workflow_pause_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    run, err = await _wf_resolve_run(args)
    if run is None:
        return err
    ok, data = await _wf_call(wf_api.pause_workflow_run_endpoint(run["id"]))
    if not ok:
        return t(f"❌ Pause failed: {data}", f"❌ 暂停失败：{data}")
    return t(
        f"⏸ Pause requested for run `{run['id']}` — it takes effect at the "
        "next safe node boundary (`/workflow-resume` continues it).",
        f"⏸ 已请求暂停 run `{run['id']}`——将在下一个安全节点边界生效"
        "（`/workflow-resume` 恢复）。",
    )


async def _workflow_resume_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    run, err = await _wf_resolve_run(args)
    if run is None:
        return err
    pi = run.get("pending_interrupt") or {}
    if run.get("status") == "paused" and pi.get("kind") in ("human", "supervisor"):
        return t(
            "This run is waiting for a human answer — reply on the Workflows "
            "page resume card (`/workflow-resume` cannot answer it for you).",
            "该 run 正在等待人工回答——请到 Workflows 页的恢复卡回答"
            "（`/workflow-resume` 无法代答）。",
        )
    ok, data = await _wf_call(wf_api.resume_workflow_run_endpoint(run["id"], {}))
    if not ok:
        return t(f"❌ Resume failed: {data}", f"❌ 恢复失败：{data}")
    return t(
        f"▶ Run `{run['id']}` resumed — `/workflow-status` follows the progress.",
        f"▶ run `{run['id']}` 已恢复——`/workflow-status` 可查进度。",
    )


async def _workflow_cancel_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    run, err = await _wf_resolve_run(args)
    if run is None:
        return err
    ok, data = await _wf_call(wf_api.cancel_workflow_run_endpoint(run["id"]))
    if not ok:
        return t(f"❌ Cancel failed: {data}", f"❌ 取消失败：{data}")
    return t(
        f"🛑 Run `{run['id']}` cancelled.",
        f"🛑 run `{run['id']}` 已取消。",
    )


async def _workflow_list_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    import time as _time

    from .. import workflows as wf_store

    defs = wf_store.list_defs()
    if not defs:
        return _wf_no_defs_reply()
    lines = [
        t(f"**Workflows** ({len(defs)})", f"**工作流**（{len(defs)} 个）"),
        "",
    ]
    for d in defs:
        n = len(d.get("steps") or [])
        runs = wf_store.list_runs(workflow_id=d.get("id"))
        if runs:
            r = runs[0]
            stamp = _time.strftime(
                "%m-%d %H:%M", _time.localtime(r.get("updated") or r.get("started") or 0)
            )
            tail = t(
                f"last run {_wf_status_label(r.get('status'))} ({stamp})",
                f"最近 run {_wf_status_label(r.get('status'))}（{stamp}）",
            )
        else:
            tail = t("never run", "未运行过")
        lock = " 🔒" if d.get("system") else ""
        lines.append(
            f"- **{d.get('name')}**{lock} `"
            + t(f"v{d.get('version')} · {n} steps", f"v{d.get('version')} · {n} 步")
            + "` · "
            + tail
        )
    return "\n".join(lines)


async def _workflow_delete_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from .. import workflows as wf_store
    from ..api import workflows as wf_api

    token = (args or "").strip()
    if not token:
        return _wf_family_usage()
    if not wf_store.list_defs():
        return _wf_no_defs_reply()
    cands = _wf_match_defs(token)
    if not cands:
        return (
            t(
                f"No workflow matches `{token}`. Existing workflows:",
                f"没有匹配 `{token}` 的工作流。现有：",
            )
            + "\n"
            + _wf_defs_table(wf_store.list_defs())
        )
    if len(cands) > 1:
        return (
            t(
                f"`{token}` matches {len(cands)} workflows — pass the exact name or id:",
                f"`{token}` 匹配到 {len(cands)} 个工作流——请用完整名称或 id 指定：",
            )
            + "\n"
            + _wf_defs_table(cands)
        )
    wf = cands[0]
    ok, data = await _wf_call(wf_api.delete_workflow_endpoint(wf["id"]))
    # delete 端点有两种失败形态：HTTPException（404）与 {"ok": False,
    # "error": "Built-in workflow…"}（is_system_def 保护）——都要摊平。
    if not ok or (isinstance(data, dict) and data.get("ok") is False):
        err = data if isinstance(data, str) else (data or {}).get("error") or "unknown"
        return t(f"❌ Delete failed: {err}", f"❌ 删除失败：{err}")
    return t(
        f"🗑 Deleted workflow **{wf.get('name')}** (`{wf['id']}`). Its run "
        "history is kept (visible on the Workflows page); the definition "
        "itself can no longer be run.",
        f"🗑 已删除工作流 **{wf.get('name')}**（`{wf['id']}`）。历史 run 记录"
        "保留（Workflows 页仍可见），该定义不可再运行。",
    )


BUILTINS: dict[str, BuiltinCommand] = {
    "help": BuiltinCommand(
        name="help",
        description=(
            "List available commands and skills",
            "列出可用命令与技能",
        ),
        handler=_help_handler,
    ),
    "compact": BuiltinCommand(
        name="compact",
        description=(
            "Manually compact session context (LLM-summarizes old messages to free tokens)",
            "手动压缩会话上下文（LLM 摘要旧消息，释放 tokens）",
        ),
        handler=_compact_handler,
        async_handler=_compact_async_handler,
    ),
    "clear": BuiltinCommand(
        name="clear",
        description=(
            "Clear this session's conversation history and start fresh (files, mounts and settings are kept)",
            "清空本会话的对话上下文，重新开始（会话文件、挂载与设置保留）",
        ),
        handler=_clear_handler,
        async_handler=_clear_async_handler,
    ),
    "dev-swap": BuiltinCommand(
        name="dev-swap",
        description=(
            "Developer hot-swap: rebuild the runtime bundle and restart onto it "
            "(tests → build → preflight → swap → verify, auto-rollback on failure)",
            "开发热切换：重建 runtime bundle 并热重启到新版本"
            "（单测 → 构建 → 预检 → 切换 → 验证，失败自动回滚）",
        ),
        handler=lambda *a, **k: t(
            "(/dev-swap runs asynchronously — just send it plain)",
            "（/dev-swap 异步执行——直接发送即可）",
        ),
        async_handler=_dev_swap_async_handler,
        # Rebuilds and restarts the runtime — channel-side requires an
        # admin userid (fail-closed: empty admin_users rejects everyone).
        channel_policy="admin",
    ),
    "fork": BuiltinCommand(
        name="fork",
        description=(
            "Copy this IM (DingTalk/WeCom) conversation into a fresh desktop session (history carried over, this chat untouched)",
            "把本 IM（钉钉/企微）对话复制为桌面普通对话（历史带走，本对话保持不变）",
        ),
        handler=_fork_handler,
        async_handler=_fork_async_handler,
    ),
    "goal": BuiltinCommand(
        name="goal",
        description=(
            "Set or show a long-running goal for this session (advances autonomously)",
            "设定或查看本会话的长程目标（自主推进）",
        ),
        handler=_goal_handler,
    ),
    "mount": BuiltinCommand(
        name="mount",
        description=(
            "Mount a local folder as session context (/mount <path> [ro|rw])",
            "挂载本地目录为本会话上下文（/mount <路径> [ro|rw]）",
        ),
        handler=_mount_handler,
    ),
    "mounts": BuiltinCommand(
        name="mounts",
        description=(
            "Show this session's mounted context folders",
            "查看本会话挂载的上下文目录",
        ),
        handler=_mounts_handler,
    ),
    "umount": BuiltinCommand(
        name="umount",
        description=(
            "Unmount a context folder (/umount <name|path>)",
            "卸载一个上下文目录（/umount <名称|路径>）",
        ),
        handler=_umount_handler,
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
    "primary": BuiltinCommand(
        name="primary",
        description=(
            "Set the primary working directory (bash cwd); /primary clear to unset",
            "设置主工作目录（bash cwd）；/primary clear 取消",
        ),
        handler=_primary_handler,
    ),
    "subagent": BuiltinCommand(
        name="subagent",
        description=(
            "Create a subagent (/subagent <goal> direct; /subagent split <task> to decompose first)",
            "创建 subagent（/subagent <goal> 直接启动；/subagent 拆分 <任务> 先 LLM 拆分再确认）",
        ),
        handler=_subagent_handler,
        async_handler=_subagent_async_handler,
    ),
    "subagent-split": BuiltinCommand(
        name="subagent-split",
        description=(
            "Split a task into multiple subagents (/subagent-split <task>, alias of /subagent split)",
            "拆分任务为多个 subagent（/subagent-split <任务>，同 /subagent 拆分）",
        ),
        handler=_subagent_handler,
        async_handler=_subagent_split_async_handler,
    ),
    "think": BuiltinCommand(
        name="think",
        description=(
            "Set or show this session's thinking effort (/think <off|low|medium|high|auto>)",
            "设置或查看本会话思考强度（/think <off|low|medium|high|auto>）",
        ),
        handler=_think_handler,
        async_handler=_think_async_handler,
    ),
    "workflow-run": BuiltinCommand(
        name="workflow-run",
        description=(
            "Start a workflow run (/workflow-run <name|id>, fuzzy name match)",
            "运行一个工作流（/workflow-run <名称|id>，名称模糊匹配）",
        ),
        handler=_wf_async_only_hint("workflow-run"),
        async_handler=_workflow_run_async_handler,
    ),
    "workflow-status": BuiltinCommand(
        name="workflow-status",
        description=(
            "Show the status of the latest (or a given) workflow run",
            "查看最近一次（或指定）工作流运行的状态",
        ),
        handler=_wf_async_only_hint("workflow-status"),
        async_handler=_workflow_status_async_handler,
    ),
    "workflow-pause": BuiltinCommand(
        name="workflow-pause",
        description=(
            "Pause the latest (or a given) running workflow run",
            "暂停最近（或指定）正在运行的工作流",
        ),
        handler=_wf_async_only_hint("workflow-pause"),
        async_handler=_workflow_pause_async_handler,
    ),
    "workflow-resume": BuiltinCommand(
        name="workflow-resume",
        description=(
            "Resume a paused workflow run (human answers still go via the Workflows page)",
            "恢复已暂停的工作流运行（人工回答仍走 Workflows 页）",
        ),
        handler=_wf_async_only_hint("workflow-resume"),
        async_handler=_workflow_resume_async_handler,
    ),
    "workflow-cancel": BuiltinCommand(
        name="workflow-cancel",
        description=(
            "Cancel the latest (or a given) workflow run",
            "取消最近（或指定）一次工作流运行",
        ),
        handler=_wf_async_only_hint("workflow-cancel"),
        async_handler=_workflow_cancel_async_handler,
    ),
    "workflow-list": BuiltinCommand(
        name="workflow-list",
        description=(
            "List workflow definitions (name · version · steps · last run)",
            "列出工作流定义（名称 · 版本 · 步骤数 · 最近运行）",
        ),
        handler=_wf_async_only_hint("workflow-list"),
        async_handler=_workflow_list_async_handler,
    ),
    "workflow-delete": BuiltinCommand(
        name="workflow-delete",
        description=(
            "Delete a workflow definition (/workflow-delete <name|id>; run history is kept)",
            "删除工作流定义（/workflow-delete <名称|id>；历史运行保留）",
        ),
        handler=_wf_async_only_hint("workflow-delete"),
        async_handler=_workflow_delete_async_handler,
    ),
}
