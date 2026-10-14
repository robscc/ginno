"""AgentTeam 编制存储（agent-team-design.md §3.2）。

一个团队 = ``teams/<team_id>.json``：主会话 + 成员实例数组 + 共享 workspace。
成员实例（AgentInstance）是「Agent 实体化」的落点——Agent 只是角色模板，
实例才有自己的 session、状态与记忆（§3.1）。

并发纪律照抄 ``session_meta._IDX_LOCK`` 的 2026-10-05 事故教训：所有写路径
串行（进程内 ``threading.RLock`` 足够——调用方横跨事件循环与后台线程）+
原子落盘（tmp + ``os.replace``），读端永远只见完整文件。

模块契约：读函数绝不抛异常（坏文件按缺失处理）；写函数失败返回 ``None``/
``False``，由调用方决定如何呈现。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from .. import paths

# 见模块 docstring：所有写路径必须串行。RLock 而非 Lock——patch 内部会调
# get_team（同一线程重入）。
_LOCK = threading.RLock()

# 成员数硬上限（§5.6）：面板可读性 + 并发 token 成本。超出提示先归档。
MAX_MEMBERS = 6

# 同团队**同时在跑**的成员 turn 上限（§5.6「成本失控」风险项，原型
# 「并发上限 3，当前 2/3」）：只限「投递新开 turn」，steer 不占额度
# （它本来就依附在跑着的 turn 上）。满了排队，turn 结束自动 drain。
MAX_CONCURRENT_RUNNING = 3


def _write(team_id: str, data: dict) -> None:
    paths.teams_dir().mkdir(parents=True, exist_ok=True)
    p = paths.team_path(team_id)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _read(team_id: str) -> dict | None:
    p = paths.team_path(team_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) and data.get("id") else None
    except (json.JSONDecodeError, OSError):
        return None


def list_teams(project_slug: str | None = None) -> list[dict]:
    """所有团队（可按 slug 过滤），按创建时间倒序。坏文件静默跳过。"""
    out: list[dict] = []
    try:
        for p in sorted(paths.teams_dir().glob("*.json")):
            if p.name.endswith(".tmp"):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or not data.get("id"):
                continue
            if project_slug and data.get("project_slug") != project_slug:
                continue
            out.append(data)
    except OSError:
        return []
    out.sort(key=lambda t: t.get("created") or 0, reverse=True)
    return out


def get_team(team_id: str) -> dict | None:
    with _LOCK:
        return _read(team_id)


def create_team(
    name: str,
    project_slug: str,
    main_session_id: str,
    workspace: str = "",
    context_folders: list[str] | None = None,
    primary_folder: str | None = None,
    leader_instance_id: str = "",
) -> dict | None:
    """建团队。``main_session_id`` 是用户 @ 成员的协作空间（§5.1）。"""
    name = (name or "").strip()
    if not name or not main_session_id:
        return None
    team_id = f"t_{uuid.uuid4().hex[:8]}"
    now = time.time()
    data = {
        "id": team_id,
        "name": name,
        "project_slug": project_slug or "default",
        "main_session_id": main_session_id,
        "leader_instance_id": leader_instance_id or "",
        "members": [],
        "shared_workspace": {
            "workspace": workspace or "",
            "context_folders": list(context_folders or []),
            "primary_folder": primary_folder or None,
        },
        "created": now,
        "updated": now,
    }
    with _LOCK:
        try:
            _write(team_id, data)
        except OSError:
            return None
    return data


def update_team(team_id: str, patch: dict) -> dict | None:
    """浅合并顶层字段（``members`` 请用成员专用函数，别整表覆盖）。"""
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return None
        for k, v in (patch or {}).items():
            if v is not None and k not in ("id", "created"):
                data[k] = v
        data["updated"] = time.time()
        try:
            _write(team_id, data)
        except OSError:
            return None
        return data


def delete_team(team_id: str) -> bool:
    """删编制文件（成员 session 的 checkpoint 由调用方决定是否级联删）。"""
    with _LOCK:
        p = paths.team_path(team_id)
        try:
            if p.exists():
                os.unlink(p)
                return True
        except OSError:
            return False
        return False


# --------------------------------------------------------------------------- #
# 成员实例（AgentInstance）
# --------------------------------------------------------------------------- #

def add_member(
    team_id: str,
    agent_id: str,
    session_id: str,
    display_name: str = "",
    role: str = "member",
    machine: str = "local",
) -> dict | None:
    """加一个成员实例。同一 agent 可多次加入（各自独立实例，§3.1）。

    返回新成员条目；超过 ``MAX_MEMBERS`` 或团队不存在时返回 ``None``。
    """
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return None
        members = data.get("members") or []
        if len(members) >= MAX_MEMBERS:
            return None
        now = time.time()
        member = {
            "instance_id": f"ai_{uuid.uuid4().hex[:8]}",
            "agent_id": agent_id,
            # 显示名与 instance_id 解耦（herdr `agent rename` 的等价物）：
            # 改名不动寻址键。缺省用 agent_id。
            "display_name": (display_name or "").strip() or agent_id,
            # 活跃会话（投递/状态都指向它）。成员可多会话（P2 产品重构
            # 2026-10-13）：session_ids 是全部历史/并行上下文，session_id 是
            # 当前活跃那条——换会话 = 换上下文，实例与记忆编制不动。
            "session_id": session_id,
            "session_ids": [session_id],
            "role": role if role in ("leader", "member") else "member",
            # 语义态与展示信息严格分离（§3.4）：status 驱动逻辑，
            # status_detail 只渲染。初始 idle。
            "status": "idle",
            "status_detail": "",
            # 每客户端独立记 seen（§3.3）：done → idle 的判据。
            "seen": {},
            # 最近一次 turn 完成的时间戳（done 的判据，status.py 步骤 5）。
            # 0 = 从没跑完过 turn → 新成员读作 idle 而非「完成待查看」。
            "last_completed": 0.0,
            # 预留多机（§5.8）：本期恒 "local"。
            "machine": machine or "local",
            "last_active": now,
            "created": now,
        }
        members.append(member)
        data["members"] = members
        data["updated"] = now
        try:
            _write(team_id, data)
        except OSError:
            return None
        return member


def get_member(team_id: str, instance_id: str) -> dict | None:
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return None
        for m in data.get("members") or []:
            if m.get("instance_id") == instance_id:
                return m
        return None


def patch_member(team_id: str, instance_id: str, patch: dict) -> dict | None:
    """改成员字段（status / status_detail / last_active / seen / display_name）。

    ``seen`` 传 dict 时按客户端键合并（不整表覆盖），其余字段直接赋值。
    """
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return None
        target = None
        for m in data.get("members") or []:
            if m.get("instance_id") == instance_id:
                for k, v in (patch or {}).items():
                    if v is None or k in ("instance_id", "created"):
                        continue
                    if k == "seen" and isinstance(v, dict):
                        m["seen"] = {**(m.get("seen") or {}), **v}
                    else:
                        m[k] = v
                m["last_active"] = time.time()
                target = m
                break
        if target is None:
            return None
        data["updated"] = time.time()
        try:
            _write(team_id, data)
        except OSError:
            return None
        return target


def member_add_session(team_id: str, instance_id: str, session_id: str) -> dict | None:
    """给成员加一条会话并设为活跃（新上下文，编制不动）。幂等去重。"""
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return None
        for m in data.get("members") or []:
            if m.get("instance_id") != instance_id:
                continue
            ids = list(m.get("session_ids") or [])
            if m.get("session_id") and m["session_id"] not in ids:
                ids.insert(0, m["session_id"])  # 兼容 P1 只有 session_id 的存量
            if session_id not in ids:
                ids.append(session_id)
            m["session_ids"] = ids
            m["session_id"] = session_id
            m["last_active"] = time.time()
            data["updated"] = time.time()
            try:
                _write(team_id, data)
            except OSError:
                return None
            return m
        return None


def member_remove_session(team_id: str, instance_id: str, session_id: str) -> dict | None:
    """移除成员的一条会话。活跃被移除时切到最新剩余；一条不剩返回 None
    （调用方应顺势 remove_member——没有会话的成员实例没有存在意义）。"""
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return None
        for m in data.get("members") or []:
            if m.get("instance_id") != instance_id:
                continue
            ids = [s for s in (m.get("session_ids") or []) if s != session_id]
            m["session_ids"] = ids
            if m.get("session_id") == session_id:
                m["session_id"] = ids[-1] if ids else ""
            m["last_active"] = time.time()
            data["updated"] = time.time()
            if not ids:
                return None  # 已写盘（session_ids 空），调用方收尾 remove_member
            try:
                _write(team_id, data)
            except OSError:
                return None
            return m
        return None


def remove_member(team_id: str, instance_id: str) -> bool:
    with _LOCK:
        data = _read(team_id)
        if data is None:
            return False
        before = len(data.get("members") or [])
        data["members"] = [
            m for m in (data.get("members") or [])
            if m.get("instance_id") != instance_id
        ]
        if len(data["members"]) == before:
            return False
        data["updated"] = time.time()
        try:
            _write(team_id, data)
        except OSError:
            return False
        return True


def list_members(team_id: str) -> list[dict]:
    with _LOCK:
        data = _read(team_id)
        return list((data or {}).get("members") or [])


# --------------------------------------------------------------------------- #
# 反查：session → team / member
# --------------------------------------------------------------------------- #

def team_of_session(session_id: str) -> tuple[dict, dict | None] | None:
    """某 session 属于哪个团队。

    返回 ``(team, member_or_None)``：``member`` 为 None 表示这是主会话本身
    （``main_session_id`` 命中），否则是该成员的条目。找不到返回 None。

    投递分支（§5.2 步骤 2）与状态回流都靠它判断「这条消息该走 team 语义
    还是普通 @agent 语义」，所以必须能同时认主会话和成员会话。
    """
    if not session_id:
        return None
    for team in list_teams():
        if team.get("main_session_id") == session_id:
            return team, None
        for m in team.get("members") or []:
            if session_id in (m.get("session_ids") or []) or m.get("session_id") == session_id:
                return team, m
    return None


def member_of_agent(team_id: str, agent_id: str) -> dict | None:
    """团队里某 agent 的**第一个**实例（@agent 只写 id 时的解析目标）。

    同一 agent 多实例时取最先加入的——用户 @dev 通常指「那个 dev」。要精确
    寻址第二个实例得用 instance_id（UI 的 @ 菜单列出全部实例，§5.1）。
    """
    for m in list_members(team_id):
        if m.get("agent_id") == agent_id:
            return m
    return None
