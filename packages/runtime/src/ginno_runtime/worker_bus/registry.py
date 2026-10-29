"""Worker 花名册与状态投影（worker-protocol-design.md §3/§5）。

状态不新造存储：由引擎现有信号**投影**——task-typed 的 meta 终态 +
进程内活跃表（``_RUNNING_TURNS`` / ``_MANAGED_TURNS`` / ``_PENDING_RESUME``
/ ``_SESSIONS``）。``_find_meta`` 是全局扫描（跨项目天然支持）。
"""

from __future__ import annotations

from typing import Any

from .. import paths
from ..server_shared import (
    _MANAGED_TURNS,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _log,
)
from ..session_meta import _find_meta, _session_meta_list

# 可被派活/订阅的会话类型。**main 已放开**（2026-10-29 用户实测「AMS 迁移」
# 场景后确认：用法就是「会话即员工」——wake/steer 注入 main 的机制本就存在
# （子代理回传/渠道消息同通道），缺的只是闸门）。channel 仍拒收：那是用户
# 在 IM 端的实时对话面，往里派活语义不通。
ASSIGNABLE_TYPES = frozenset({"subagent", "delegation", "scheduled", "main"})

# 有终态语义的 task-typed 会话（watch want=terminal 的合法目标；main 无终态，
# 只能 digest）。subagent/delegation/scheduled 有明确 settle 时刻。
TASK_TYPES = frozenset({"subagent", "delegation", "scheduled"})


def _mtype(meta: dict) -> str:
    """meta 的类型归一。2026-10-29 起创建即主动标注 ``"main"``(api/sessions
    写入 + 启动 backfill 迁移历史行),本函数的空值→main 只是**legacy 兜底**
    (迁移前的索引/旧 checkpoint 里 main 的 type 是 null)——判型出口统一走
    这里,兜底语义集中一处。"""
    return str(meta.get("type") or "").strip() or "main"


def _assignable(meta: dict) -> bool:
    """是否可被派活:类型白名单 + 用户「接活」开关。开关只对 main 生效
    (扇形菜单标注,meta ``worker`` 字段;缺省接活——main 本身也是 worker),
    task-typed 天生 worker 不受开关控制。"""
    t = _mtype(meta)
    if t not in ASSIGNABLE_TYPES:
        return False
    return True if t in TASK_TYPES else meta.get("worker") is not False

# 投影后的终态口径（subagent 的 done/failed/stopped + 通用 failed）。
TERMINAL_PROJECTED = frozenset({"done", "failed", "stopped"})


def project_status(session_id: str, meta: dict | None = None) -> str:
    """协议状态机的投影实现：

    terminal（done/failed/stopped，来自 meta 终态）> waiting（停在
    interrupt 卡）> working（有活跃 turn）> idle（进程内已加载、无 turn）
    > unknown（磁盘有 meta、进程内无会话——重启后未唤醒）> missing。
    """
    found = None if meta is not None else _find_meta(session_id)
    if meta is None:
        if not found:
            return "missing"
        meta = found[0]
    t = str(meta.get("type") or "")
    if t == "subagent":
        sa = meta.get("subagent") or {}
        st = str(sa.get("status") or "")
        if st in TERMINAL_PROJECTED:
            return st
    elif t == "delegation":
        # stop_reason 是 delegation meta 的顶层字段（delegation_sessions.py）
        sr = str(meta.get("stop_reason") or "")
        if sr and sr != "running":
            return "done" if sr == "success" else "failed"
    # 活跃信号（跨类型通用；scheduled 无 meta 终态字段，只看这里）
    if session_id in _PENDING_RESUME:
        return "waiting"
    if session_id in _RUNNING_TURNS or session_id in _MANAGED_TURNS:
        return "working"
    if session_id in _SESSIONS:
        return "idle"
    return "unknown"


def _row(meta: dict, slug: str) -> dict:
    sid = str(meta.get("id") or "")
    return {
        "session_id": sid,
        "title": str(meta.get("title") or ""),
        "type": _mtype(meta),
        "status": project_status(sid, meta),
        "parent_session_id": meta.get("parent_session_id"),
        "project": slug,
        "updated": meta.get("updated"),
        # 摘要只给 done 的（活着的看状态即可，省 token）
        "result_summary": (
            str((meta.get("subagent") or {}).get("result_summary") or "")
            if project_status(sid, meta) == "done"
            else ""
        )[:200],
    }


def list_worker_rows(
    slug: str | None = None,
    query: str = "",
    type_filter: str = "",
    status_filter: str = "",
    limit: int = 20,
    all_projects: bool = True,
) -> list[dict]:
    """花名册行（updated 倒序）。默认**全局**（项目概念在 Ginno 已弱化,
    寻址 resolve_target 本就跨项目——花名册与寻址同口径,行内 ``project``
    字段标注归属）;``all_projects=False`` 收窄回单项目。"""
    slugs = []
    if slug and not all_projects:
        slugs = [slug]
    else:
        slugs = sorted(
            p.parent.parent.name for p in paths.home().glob("projects/*/sessions/_index.json")
        )
    q = (query or "").strip().lower()
    tf = (type_filter or "").strip().lower()
    sf = (status_filter or "").strip().lower()
    rows: list[dict] = []
    for s in slugs:
        for m in _session_meta_list(s):
            if not _assignable(m):
                continue
            row = _row(m, s)
            if q and q not in (
                row["title"].lower() + " " + row["session_id"].lower()
            ):
                continue
            if tf and tf != row["type"]:
                continue
            if sf and sf != row["status"]:
                continue
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
    return rows[: max(1, min(int(limit or 20), 50))]


def resolve_target(target: str) -> tuple[str, dict | None, str]:
    """(session_id, meta, error)。target 直接是 id 时精确命中；否则在
    assignable 里按标题前缀/子串匹配，唯一命中才接受（歧义报错并列出
    候选——模型下一轮可自行纠正）。跨项目天然支持（_find_meta 全局）。"""
    t = (target or "").strip()
    if not t:
        return "", None, "target is required"
    # 1) 精确 id
    found = _find_meta(t)
    if found:
        meta, slug = found
        if not _assignable(meta):
            if _mtype(meta) == "main" and meta.get("worker") is False:
                return "", None, (
                    f"session {t[:18]} is marked not accepting assignments "
                    "(user turned it off); pick another worker via list_workers"
                )
            return "", None, (
                f"session {t[:18]} is not assignable (type="
                f"{_mtype(meta)!r}); assignable types: "
                f"{sorted(ASSIGNABLE_TYPES)}"
            )
        return t, meta, ""
    # 2) 标题匹配（assignable 内，唯一才收）
    tl = t.lower()
    cands: list[dict] = []
    for p in paths.home().glob("projects/*/sessions/_index.json"):
        s = p.parent.parent.name
        for m in _session_meta_list(s):
            if not _assignable(m):
                continue
            title = str(m.get("title") or "").lower()
            if tl in title or title.startswith(tl):
                cands.append(_row(m, s))
    if len(cands) == 1:
        cid = cands[0]["session_id"]
        found = _find_meta(cid)
        return cid, (found[0] if found else None), ""
    if len(cands) > 1:
        _log.info("worker_target_ambiguous q=%s hits=%d", t, len(cands))
        listing = "\n".join(
            f"- {c['session_id']} [{c['status']}] {c['title'][:40]}" for c in cands[:6]
        )
        return "", None, f"ambiguous target, multiple matches:\n{listing}"
    return "", None, f"no worker session matches {t[:40]!r}"
