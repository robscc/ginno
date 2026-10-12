"""成员语义态聚合 + 状态上卷（agent-team-design.md §3.3 / §5.3）。

**唯一权威来源**（§5.9 的纪律）：HTTP API、成员面板、总览页、以及将来的
``list_members(wait=True)`` 工具全部读这里，任何一层都不得自己复制一份状态
判断逻辑——herdr 之所以能让「agent 等到另一个 agent 真的 blocked」，正因为
``agent wait`` 和 UI 读的是同一个服务端状态。

判态**零新事件、纯聚合现有信号**：

| 态 | 判据 |
|---|---|
| ``blocked`` | 该 session 停在 permission / ask_user interrupt（``_PENDING_RESUME``） |
| ``working`` | 该 session 有活 turn（``_RUNNING_TURNS`` 或 ``_TURN_TASKS`` 未完成） |
| ``waiting`` | 自己 turn 已结束但还有运行中的子代（subagent 完成门，§5.6） |
| ``done``    | turn 结束且**该客户端尚未 seen**（§3.3 收件箱语义） |
| ``idle``    | 无 turn、无 pending、无子代、已 seen |
| ``unknown`` | 状态不可判定：进程重启后 turn 未愈合（``stop_reason`` 缺失/running） |
| ``error``   | 重试耗尽（``stop_reason == "error"``） |

``status_detail`` 是**纯展示**文本（§3.4），绝不参与任何判断——它会随每个
工具调用变化，若进逻辑状态机会抖动。
"""

from __future__ import annotations

import time

from ..server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _TURN_TASKS,
)
from ..session_meta import _session_meta_children
from . import store

# 上卷优先级（§5.3.1）：blocked 最高——「谁卡住了」必须压过一切；unknown
# 次之（不可判定也需要人看一眼）；working 再次；done（未读）随后。
_PRIORITY = ("blocked", "unknown", "error", "working", "waiting", "done", "idle")

# 子代仍在跑的态（waiting 的判据）。与 subagent 调度器的 LIVE_STATUSES 同义，
# 但这里独立声明：team 不 import subagent_scheduler 的私有常量，避免耦合。
_LIVE_CHILD = ("running", "waiting")


def _has_live_child(slug: str, session_id: str) -> bool:
    """该 session 是否还有运行中的子代（subagent / delegation）。"""
    try:
        for m in _session_meta_children(slug, session_id):
            sa = m.get("subagent") or {}
            if str(sa.get("status") or "") in _LIVE_CHILD:
                return True
            # delegation 用 stop_reason 记态（delegation_sessions.py）。
            if m.get("type") == "delegation" and m.get("stop_reason") == "running":
                return True
    except Exception:  # noqa: BLE001 — 判态失败不该炸整个面板
        return False
    return False


def _is_live_turn(session_id: str) -> bool:
    """有活 turn：在跑，或 turn 任务还没结束（parked 的也算活着，但它会先被
    blocked 判据截走）。"""
    if session_id in _RUNNING_TURNS:
        return True
    task = _TURN_TASKS.get(session_id)
    return task is not None and not task.done()


def member_status(
    slug: str, session_id: str, member: dict, client: str = "main"
) -> tuple[str, str]:
    """算一个成员实例的 ``(语义态, 展示文本)``。

    ``client`` 是 seen 的键（§3.3：主窗口与 pin 浮窗各自记，徽标可以不同）。
    """
    if not session_id:
        return "unknown", "无 session"

    # 1) blocked 优先：停在 interrupt 等人回答。这是最有价值的一态——
    #    Ginno 用结构化事件判，不是 herdr 那样读屏幕猜，所以零误判（§3.3）。
    if session_id in _PENDING_RESUME:
        kind = str(_PENDING_KIND.get(session_id) or "permission")
        detail = {
            "permission": "等待权限确认",
            "ask_user": "等你回答问题",
            "version_propose": "等待版本确认",
        }.get(kind, f"等待确认（{kind}）")
        return "blocked", detail

    # 2) working：有活 turn。展示文本取最后一条工具调用的可读标签（§5.3）。
    if _is_live_turn(session_id):
        return "working", _working_detail(session_id)

    # 3) error / unknown：从 session meta 的终态标记读。
    meta = _SESSIONS.get(session_id) or {}
    stop_reason = str(meta.get("stop_reason") or "")
    if stop_reason == "error":
        return "error", "重试耗尽"
    # 进程重启后 turn 未愈合：meta 还写着 running 但进程里没有任何活 turn
    # → 状态不可判定。绝不 fallback 到 idle（§3.3，herdr 同款纪律）。
    if stop_reason == "running":
        return "unknown", "重启时 turn 未愈合 — 状态不可判定"

    # 4) waiting：自己没 turn 了，但还有子代在跑（完成门，§5.6）。
    if _has_live_child(slug, session_id):
        return "waiting", "子任务仍在运行"

    # 5) done vs idle：**有一次完成的产出**且本客户端还没看过 → done（收件箱
    #    语义，§3.3）。判据是 last_completed 而不是 last_active：刚加入、从没
    #    跑过 turn 的成员必须读作 idle，否则每个新成员一上来就顶着「完成待
    #    查看」的未读徽标，needs_action 恒真，上卷信号就废了。
    completed = float(member.get("last_completed") or 0)
    seen_at = float((member.get("seen") or {}).get(client) or 0)
    if completed and seen_at < completed:
        return "done", _done_detail(member)

    return "idle", _idle_detail(member)


def _working_detail(session_id: str) -> str:
    """正在干什么：取 session 里最后记录的工具名（若有）。

    不额外调 LLM、不读 checkpoint——只取内存里现成的字段。拿不到就返回通用
    文案，前端可以再用 ``toolLabels`` 映射美化（§5.3）。
    """
    entry = _SESSIONS.get(session_id) or {}
    tool = str(entry.get("last_tool") or "").strip()
    return f"正在调用 {tool}" if tool else "正在运行"


def _done_detail(member: dict) -> str:
    detail = str(member.get("status_detail") or "").strip()
    return detail or "已完成，待查看"


def _idle_detail(member: dict) -> str:
    detail = str(member.get("status_detail") or "").strip()
    return detail or "空闲"


def mark_seen(team_id: str, instance_id: str, client: str = "main") -> dict | None:
    """用户查看了该成员工作台 → 记 seen（done → idle，§3.3）。

    只有**显式聚焦**才算 seen（herdr：*"explicit pane focus / agent focus mark
    it seen, reads do not"*）——后台拉一次状态不算看过。
    """
    return store.patch_member(team_id, instance_id, {"seen": {client: time.time()}})


def refresh_team(team_id: str, client: str = "main") -> dict | None:
    """重算全部成员语义态并落盘，返回更新后的团队。

    落盘是为了让「重启后仍能显示 done 未读」成立（§5.8）——纯内存态一重启
    就没了，收件箱语义会丢。
    """
    team = store.get_team(team_id)
    if team is None:
        return None
    slug = str(team.get("project_slug") or "default")
    changed = False
    for m in team.get("members") or []:
        status, detail = member_status(slug, m.get("session_id") or "", m, client)
        if m.get("status") != status or m.get("status_detail") != detail:
            store.patch_member(team_id, m["instance_id"], {
                "status": status,
                "status_detail": detail,
            })
            changed = True
    return store.get_team(team_id) if changed else team


def rollup(team: dict, client: str = "main") -> dict:
    """状态上卷（§5.3.1）：团队级摘要。

    返回 ``{status, counts, needs_action, working, blocked, done_unread}``。
    ``status`` 取全部成员的最高优先级态；``needs_action`` 是「该不该打扰用户」
    的判据——只有 blocked 与 done 为真（working 止于团队行，不冒泡到 Dock）。
    """
    members = (team or {}).get("members") or []
    counts: dict[str, int] = {}
    for m in members:
        s = str(m.get("status") or "idle")
        counts[s] = counts.get(s, 0) + 1

    top = "idle"
    for s in _PRIORITY:
        if counts.get(s):
            top = s
            break

    blocked = counts.get("blocked", 0)
    done_unread = counts.get("done", 0)
    return {
        "status": top,
        "counts": counts,
        "total": len(members),
        "working": counts.get("working", 0),
        "blocked": blocked,
        "done_unread": done_unread,
        # 只有需要用户行动的态才向上冒泡到全局徽标（§5.3.1 纪律 2）。
        "needs_action": bool(blocked or done_unread),
    }


def member_row(team: dict, member: dict, client: str = "main") -> dict:
    """成员面板 / 总览卡的一行（前端直接渲染，不再二次计算）。"""
    slug = str((team or {}).get("project_slug") or "default")
    status, detail = member_status(
        slug, member.get("session_id") or "", member, client
    )
    last_active = float(member.get("last_active") or member.get("created") or 0)
    return {
        "instance_id": member.get("instance_id"),
        "agent_id": member.get("agent_id"),
        "display_name": member.get("display_name") or member.get("agent_id"),
        "session_id": member.get("session_id"),
        "role": member.get("role") or "member",
        "machine": member.get("machine") or "local",
        "status": status,
        "status_detail": detail,
        "last_active": last_active,
        "elapsed_s": round(max(0.0, time.time() - last_active), 1),
        "unread": status == "done",
    }


def team_payload(team: dict, client: str = "main") -> dict:
    """团队的完整前端载荷：编制 + 成员行 + 上卷摘要。"""
    members = [member_row(team, m, client) for m in (team or {}).get("members") or []]
    payload = dict(team or {})
    payload["member_rows"] = members
    payload["rollup"] = rollup({**payload, "members": [
        {"status": r["status"]} for r in members
    ]}, client)
    return payload
