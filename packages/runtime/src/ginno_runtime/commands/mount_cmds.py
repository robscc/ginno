"""Mount domain — /mount /mounts /umount /primary.

Context-folder mounts and the primary working directory (bash cwd). All
mutations go through api.sessions.apply_session_context (lazy import:
avoid import cycle).
"""

from __future__ import annotations

from ..lang import t
from ._base import BuiltinCommand


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


# 本域注册块：registry.py 装配 BUILTINS 时收集（命令清单契约快照钉住）。
COMMANDS: dict[str, BuiltinCommand] = {
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
}
