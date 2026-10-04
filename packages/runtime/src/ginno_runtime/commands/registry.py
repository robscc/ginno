"""Built-in slash command registry.

Built-ins short-circuit the turn: the handler renders a reply directly and the
graph never runs (no LLM call, no checkpoint write). V1 ships only ``/help``;
``/new``, ``/clear`` etc. are reserved names for future built-ins.

Skills are NOT registered here — they live on disk (SKILL.md) and are resolved
by the resolver via ``SkillLoader``. Names collide → built-in wins (documented
in docs/commands-and-mentions-design.md).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
        return t(
            "The current turn is paused at a confirmation step (permission / question); finish it before running /compact",
            "当前回合停在确认步骤（权限 / 提问）上；请先完成再运行 /compact",
        )
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
        stats = await maybe_compact_history(session, config, force=True)
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
}
