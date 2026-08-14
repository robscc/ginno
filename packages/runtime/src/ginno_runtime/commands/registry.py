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


def _mounts_list(session) -> list[dict]:
    """Current mounts of a live session (resolved context-dir dicts)."""
    return list((session or {}).get("context_dirs") or [])


def _fmt_mounts(session) -> str:
    from .. import context_folders as cf

    dirs = _mounts_list(session)
    primary_path = (session or {}).get("primary_path") or ""
    if not dirs:
        return "（本会话未挂载任何上下文目录）"
    lines = ["**本会话挂载的上下文目录**", ""]
    for d in dirs:
        if d.get("missing"):
            lines.append(f"- ❓ {d.get('id')}（目录缺失，已失效）")
            continue
        tag = "rw" if d.get("access") == "rw" else "ro 只读"
        star = " ★primary" if primary_path and d.get("path") == primary_path else ""
        rf = cf.rule_file_for(d["path"])
        rule = f"（{rf.name}）" if rf else ""
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


_MOUNT_USAGE = (
    "**上下文目录挂载用法**\n"
    "- `/mount <路径> [ro|rw]` — 挂载目录（自动注册进目录库，默认 rw）\n"
    "- `/mount` / `/mounts` — 查看当前挂载\n"
    "- `/umount <名称|路径>` — 卸载\n"
    "- `/primary <名称|路径>` — 设为主工作目录（bash 的 cwd）\n"
    "- `/primary clear` — 取消主工作目录\n"
    "目录库与访问级也可在 设置 → 上下文目录 管理。"
)


def _mount_handler(project_slug: str | None = None, session=None, args=None) -> str:
    from .. import context_folders as cf

    if not session:
        return _MOUNT_USAGE
    args = (args or "").strip()
    if not args:
        return _fmt_mounts(session) + "\n\n" + _MOUNT_USAGE

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
        return f"挂载失败：{probe.get('error')}"
    folder = cf.add_folder(path, access=access, load_rules=True)

    dirs = _mounts_list(session)
    ids = [d["id"] for d in dirs if not d.get("missing")]
    if folder["id"] in ids:
        return (
            f"目录已在挂载中：**{folder['name']}** `{folder['path']}`\n\n"
            + _fmt_mounts(session)
        )
    ids.append(folder["id"])
    # First mount becomes primary automatically (bash cwd switches to it);
    # later mounts never steal an existing primary.
    primary = session.get("primary_folder") or (folder["id"] if not dirs else None)

    from ..api.sessions import apply_session_context  # lazy: avoid import cycle

    res = apply_session_context(session.get("session_id", ""), ids, primary)
    if not res.get("ok"):
        return f"挂载失败：{res.get('error')}"
    star = "，已设为主工作目录（bash 的 cwd）" if primary == folder["id"] and not dirs else ""
    rf = probe.get("rule_file")
    rule_note = f"；检测到 {rf}，其规则将注入上下文" if rf else ""
    return (
        f"✅ 已挂载 **{folder['name']}** `{folder['path']}`"
        f"（{folder['access']}{star}）{rule_note}\n\n" + _fmt_mounts(session)
    )


def _mounts_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _MOUNT_USAGE
    return _fmt_mounts(session)


def _umount_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _MOUNT_USAGE
    token = (args or "").strip()
    if not token:
        return "用法：`/umount <名称|路径>`\n\n" + _fmt_mounts(session)
    dirs = _mounts_list(session)
    hit = _match_mount(dirs, token)
    if not hit:
        return f"未找到挂载：{token}\n\n" + _fmt_mounts(session)
    ids = [d["id"] for d in dirs if not d.get("missing") and d["id"] != hit["id"]]
    primary = session.get("primary_folder")
    if primary == hit["id"]:
        primary = None
    from ..api.sessions import apply_session_context  # lazy: avoid import cycle

    res = apply_session_context(session.get("session_id", ""), ids, primary)
    if not res.get("ok"):
        return f"卸载失败：{res.get('error')}"
    return f"已卸载 **{hit.get('name')}** `{hit.get('path')}`\n\n" + _fmt_mounts(session)


def _primary_handler(project_slug: str | None = None, session=None, args=None) -> str:
    if not session:
        return _MOUNT_USAGE
    token = (args or "").strip()
    dirs = _mounts_list(session)
    if not token:
        return "用法：`/primary <名称|路径>` 或 `/primary clear`\n\n" + _fmt_mounts(session)
    ids = [d["id"] for d in dirs if not d.get("missing")]
    if token.lower() == "clear":
        primary = None
    else:
        hit = _match_mount(dirs, token)
        if not hit:
            return f"未找到挂载：{token}（先用 /mount 挂载）"
        primary = hit["id"]
    from ..api.sessions import apply_session_context  # lazy: avoid import cycle

    res = apply_session_context(session.get("session_id", ""), ids, primary)
    if not res.get("ok"):
        return f"设置失败：{res.get('error')}"
    if primary is None:
        return "已取消主工作目录（cwd 恢复为会话文件目录）\n\n" + _fmt_mounts(session)
    pp = res.get("primary_path") or ""
    return f"主工作目录已设为 `{pp}`（bash 的 cwd 与相对路径基准）\n\n" + _fmt_mounts(session)


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
    "mount": BuiltinCommand(
        name="mount",
        description="挂载本地目录为本会话上下文（/mount <路径> [ro|rw]）",
        handler=_mount_handler,
    ),
    "mounts": BuiltinCommand(
        name="mounts",
        description="查看本会话挂载的上下文目录",
        handler=_mounts_handler,
    ),
    "umount": BuiltinCommand(
        name="umount",
        description="卸载一个上下文目录（/umount <名称|路径>）",
        handler=_umount_handler,
    ),
    "primary": BuiltinCommand(
        name="primary",
        description="设置主工作目录（bash cwd）；/primary clear 取消",
        handler=_primary_handler,
    ),
}
