"""AgentTeam 端点（agent-team-design.md §5.1 / §6 P0）。

- GET    /api/teams                       — 团队列表（可按 slug 过滤，带上卷摘要）
- POST   /api/teams                       — 建团队（同时建主会话 session）
- GET    /api/teams/{id}                  — 团队详情（成员行 + rollup）
- PATCH  /api/teams/{id}                  — 改名 / 换 leader / 改共享 workspace
- DELETE /api/teams/{id}                  — 解散（成员 session 级联删）
- POST   /api/teams/{id}/members          — 加成员实例（建其独立 session）
- PATCH  /api/teams/{id}/members/{iid}    — 改显示名 / 角色
- DELETE /api/teams/{id}/members/{iid}    — 移除成员（停 turn + 删 session）
- POST   /api/teams/{id}/members/{iid}/seen   — 标记已查看（done → idle）
- POST   /api/teams/{id}/deliver          — @ 投递（三态回执，§5.3.3）

状态一律经 :mod:`..teams.status` 计算（§5.9 唯一权威来源），本模块不自己判态。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from .. import server_shared as shared
from ..server_shared import _log
from ..teams import status as team_status
from ..teams import deliver as team_deliver
from ..teams import store as team_store

router = APIRouter()


class CreateTeamRequest(BaseModel):
    name: str
    project_slug: str = "default"
    # 主会话 = 用户 @ 成员的协作空间（§5.1）。不传则自动建一个。
    main_session_id: str | None = None
    agent_id: str | None = None          # 主会话的 Lead persona（待决问题 2）
    workspace: str = ""
    context_folders: list[str] = []
    primary_folder: str | None = None


class AddMemberRequest(BaseModel):
    agent_id: str
    display_name: str = ""
    role: str = "member"                 # "leader" | "member"


class DeliverRequest(BaseModel):
    instance_id: str = ""
    # 只给 agent_id 时解析为该 agent 的第一个实例（§5.1 @ 菜单列全部实例）。
    agent_id: str = ""
    text: str
    wait: bool = True


def _payload(team: dict, client: str = "main") -> dict:
    return team_status.team_payload(team, client)


async def _broadcast(team_id: str, client: str = "main") -> None:
    """推 team.changed 给主会话的所有 socket（成员面板 + 总览页共用）。"""
    team = team_store.get_team(team_id)
    if team is None:
        return
    await shared._push_session_event(
        str(team.get("main_session_id") or ""),
        "team.changed",
        {"team_id": team_id, "team": _payload(team, client)},
    )


# --------------------------------------------------------------------------- #
# 团队 CRUD
# --------------------------------------------------------------------------- #

@router.get("/api/teams")
async def list_teams_endpoint(project_slug: str | None = None, client: str = "main") -> dict:
    teams = []
    for t in team_store.list_teams(project_slug):
        team_status.refresh_team(t["id"], client)
        teams.append(_payload(team_store.get_team(t["id"]) or t, client))
    return {"ok": True, "teams": teams}


@router.post("/api/teams")
async def create_team_endpoint(req: CreateTeamRequest) -> dict:
    from .sessions import CreateSessionRequest, create_session  # lazy: cycle

    name = (req.name or "").strip()
    if not name:
        return {"ok": False, "error": "团队名不能为空"}

    main_sid = (req.main_session_id or "").strip()
    if not main_sid:
        # 自动建主会话。type="team" 让侧栏把它和成员 session 归到团队分区
        # （§3.2），并复用 parent_session_id 树的级联删除。
        resp = await create_session(CreateSessionRequest(
            project_slug=req.project_slug or "default",
            workspace=req.workspace or "",
            agent_id=req.agent_id or None,
            title=f"👥 {name}",
            type="team",
            context_folders=list(req.context_folders or []),
            primary_folder=req.primary_folder,
        ))
        if not resp.get("ok"):
            return {"ok": False, "error": str(resp.get("error") or "主会话创建失败")}
        main_sid = str(resp["id"])

    team = team_store.create_team(
        name=name,
        project_slug=req.project_slug or "default",
        main_session_id=main_sid,
        workspace=req.workspace or "",
        context_folders=list(req.context_folders or []),
        primary_folder=req.primary_folder,
    )
    if team is None:
        return {"ok": False, "error": "团队编制写入失败"}
    _log.info("team_created id=%s name=%r main=%s", team["id"], name, main_sid)
    return {"ok": True, "team": _payload(team)}


@router.get("/api/teams/{team_id}")
async def get_team_endpoint(team_id: str, client: str = "main") -> dict:
    team_status.refresh_team(team_id, client)
    team = team_store.get_team(team_id)
    if team is None:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "team": _payload(team, client)}


@router.patch("/api/teams/{team_id}")
async def patch_team_endpoint(team_id: str, data: dict) -> dict:
    allowed = {k: v for k, v in (data or {}).items()
               if k in ("name", "leader_instance_id", "shared_workspace", "archived")}
    team = team_store.update_team(team_id, allowed)
    if team is None:
        return {"ok": False, "error": "not found"}
    _log.info("team_patched team=%s fields=%s", team_id, ",".join(sorted(allowed)))
    await _broadcast(team_id)
    return {"ok": True, "team": _payload(team)}


@router.delete("/api/teams/{team_id}")
async def delete_team_endpoint(team_id: str, force: int = 0) -> dict:
    """解散团队（防呆三重，2026-10-13 用户要求）：

    1. **运行中拒绝**：任何成员（任意会话）或主会话有活 turn → 拒绝并点名
       是谁，``?force=1`` 才放行。误删一个正在干活的团队是最贵的事故。
    2. **主会话一并删**：roster 删掉后 type=team 的主会话会被主侧栏过滤、
       /team 也找不到它——不删就永久搁浅。成员会话 parent=主会话，级联删
       主会话即覆盖全部成员会话（多会话成员也含在内）。
    3. 归档≠删除：只想收起用 PATCH archived=true（数据全保留）。
    """
    from .sessions import delete_session  # lazy: cycle
    from ..teams import status as team_status

    team = team_store.get_team(team_id)
    if team is None:
        return {"ok": False, "error": "not found"}

    # 防呆 1：活 turn 检查（成员全部会话 + 主会话）。
    live: list[str] = []
    main_sid = str(team.get("main_session_id") or "")
    for m in team_store.list_members(team_id):
        # 成员的**全部**会话都要查（多会话成员可能在非活跃会话里跑着）。
        sids = {*(m.get("session_ids") or []), m.get("session_id") or ""}
        if any(team_status._is_live_turn(s) for s in sids if s):
            live.append(str(m.get("display_name") or m.get("agent_id") or "?"))
    if team_status._is_live_turn(main_sid):
        live.append("(main)")
    if live and not force:
        _log.warning("team_delete_refused_running team=%s live=%s", team_id, live)
        return {"ok": False, "error": "team has running members", "live": live}

    # 防呆 2：级联删主会话 = 成员会话（含多会话）全删；roster 最后删。
    try:
        if main_sid:
            await delete_session(main_sid, cascade=True)
    except Exception:  # noqa: BLE001 — 主会话删不掉也不留孤儿 roster
        _log.exception("team_main_delete_failed team=%s main=%s", team_id, main_sid)
    ok = team_store.delete_team(team_id)
    _log.info("team_deleted id=%s ok=%s force=%s", team_id, ok, force)
    return {"ok": ok}


# --------------------------------------------------------------------------- #
# 成员实例
# --------------------------------------------------------------------------- #

@router.post("/api/teams/{team_id}/members")
async def add_member_endpoint(team_id: str, req: AddMemberRequest) -> dict:
    """加成员实例：建它自己的 session（独立 checkpoint / 上下文 / 记忆，§3.1）。

    同一 agent 可多次加入，每次都是独立实例（``instance_id`` 不同）。
    """
    from .sessions import CreateSessionRequest, create_session  # lazy: cycle

    team = team_store.get_team(team_id)
    if team is None:
        return {"ok": False, "error": "not found"}
    agent_id = (req.agent_id or "").strip()
    if not agent_id:
        return {"ok": False, "error": "agent_id 必填"}
    if len(team_store.list_members(team_id)) >= team_store.MAX_MEMBERS:
        return {"ok": False, "error": f"成员数已达上限（{team_store.MAX_MEMBERS}），请先归档不活跃成员"}
    # 一个团队只能有一个领队（2026-10-14 用户需求）：已有领队再任命 → 拒绝，
    # 先把现任改回 member。
    if req.role == "leader" and (
        team.get("leader_instance_id")
        or any(m.get("role") == "leader" for m in team_store.list_members(team_id))
    ):
        return {"ok": False, "error": "团队已有领队；先把现任领队改为成员，再任命新领队"}

    sw = team.get("shared_workspace") or {}
    slug = str(team.get("project_slug") or "default")
    main_sid = str(team.get("main_session_id") or "")
    display = (req.display_name or "").strip() or agent_id

    # 成员 session 的 parent 指向主会话 → 复用 _session_meta_descendants 的
    # 级联删除与侧栏树（§3.2），零新机制。
    resp = await create_session(CreateSessionRequest(
        project_slug=slug,
        workspace=str(sw.get("workspace") or ""),
        agent_id=agent_id,
        title=f"{display} · {team.get('name') or ''}".strip(),
        type="team-member",
        context_folders=list(sw.get("context_folders") or []),
        primary_folder=sw.get("primary_folder"),
        parent_session_id=main_sid or None,
    ))
    if not resp.get("ok"):
        return {"ok": False, "error": str(resp.get("error") or "成员 session 创建失败")}

    member = team_store.add_member(
        team_id, agent_id, str(resp["id"]),
        display_name=display,
        role=("leader" if req.role == "leader" else "member"),
    )
    if member is None:
        # 编制写不进去就把刚建的 session 收掉，别留孤儿。
        try:
            from .sessions import delete_session
            await delete_session(str(resp["id"]))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "成员编制写入失败（可能已达上限）"}

    if req.role == "leader" and not team.get("leader_instance_id"):
        team_store.update_team(team_id, {"leader_instance_id": member["instance_id"]})

    _log.info(
        "team_member_added team=%s instance=%s agent=%s session=%s",
        team_id, member["instance_id"], agent_id, resp["id"],
    )
    await _broadcast(team_id)
    return {"ok": True, "member": member, "team": _payload(team_store.get_team(team_id) or team)}


@router.patch("/api/teams/{team_id}/members/{instance_id}")
async def patch_member_endpoint(team_id: str, instance_id: str, data: dict) -> dict:
    allowed = {k: v for k, v in (data or {}).items()
               if k in ("display_name", "role", "session_id")}
    if allowed.get("role") == "leader":
        team = team_store.get_team(team_id)
        cur = team_store.get_member(team_id, instance_id)
        already = (team or {}).get("leader_instance_id") or any(
            m.get("role") == "leader"
            for m in team_store.list_members(team_id)
            if m.get("instance_id") != instance_id
        )
        if already and cur and cur.get("role") != "leader":
            return {"ok": False, "error": "团队已有领队；先把现任领队改为成员，再任命新领队"}
    # 切换活跃会话（P2 多会话）：session_id 必须在该成员的 session_ids 里。
    if "session_id" in allowed:
        member = team_store.get_member(team_id, instance_id)
        if member is None:
            return {"ok": False, "error": "not found"}
        if allowed["session_id"] not in (member.get("session_ids") or []):
            return {"ok": False, "error": "session 不属于该成员"}
    member = team_store.patch_member(team_id, instance_id, allowed)
    if member is None:
        return {"ok": False, "error": "not found"}
    await _broadcast(team_id)
    return {"ok": True, "member": member}


@router.delete("/api/teams/{team_id}/members/{instance_id}")
async def remove_member_endpoint(team_id: str, instance_id: str) -> dict:
    from .sessions import delete_session  # lazy: cycle

    member = team_store.get_member(team_id, instance_id)
    if member is None:
        return {"ok": False, "error": "not found"}
    for sid in {*(member.get("session_ids") or []), member.get("session_id") or ""}:
        if sid:
            try:
                await delete_session(sid)   # 内部含 stop 级联
            except Exception:  # noqa: BLE001
                _log.exception("team_member_session_delete_failed session=%s", sid)
    ok = team_store.remove_member(team_id, instance_id)
    _log.info("team_member_removed team=%s instance=%s ok=%s", team_id, instance_id, ok)
    await _broadcast(team_id)
    return {"ok": ok}


# ---- 成员多会话（P2 产品重构 2026-10-13）------------------------------------

async def _create_member_session(team: dict, agent_id: str, display: str) -> dict:
    """按编制给成员建一条新会话（与 add_member 同参，供「新会话」复用）。"""
    from .sessions import CreateSessionRequest, create_session  # lazy: cycle

    sw = team.get("shared_workspace") or {}
    return await create_session(CreateSessionRequest(
        project_slug=str(team.get("project_slug") or "default"),
        workspace=str(sw.get("workspace") or ""),
        agent_id=agent_id,
        title=f"{display} · {team.get('name') or ''}".strip(),
        type="team-member",
        context_folders=list(sw.get("context_folders") or []),
        primary_folder=sw.get("primary_folder"),
        parent_session_id=str(team.get("main_session_id") or "") or None,
    ))


@router.post("/api/teams/{team_id}/members/{instance_id}/sessions")
async def add_member_session_endpoint(team_id: str, instance_id: str) -> dict:
    """给成员开新会话（新上下文）：建 session + 设为活跃。编制不动。"""
    team = team_store.get_team(team_id)
    member = team_store.get_member(team_id, instance_id)
    if team is None or member is None:
        return {"ok": False, "error": "not found"}
    resp = await _create_member_session(
        team, str(member.get("agent_id") or ""), str(member.get("display_name") or "")
    )
    if not resp.get("ok"):
        return {"ok": False, "error": str(resp.get("error") or "会话创建失败")}
    updated = team_store.member_add_session(team_id, instance_id, str(resp["id"]))
    if updated is None:
        try:
            from .sessions import delete_session
            await delete_session(str(resp["id"]))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "编制写入失败"}
    _log.info("team_member_session_added team=%s instance=%s session=%s",
              team_id, instance_id, resp["id"])
    await _broadcast(team_id)
    return {"ok": True, "session_id": resp["id"], "member": updated}


@router.delete("/api/teams/{team_id}/members/{instance_id}/sessions/{session_id}")
async def delete_member_session_endpoint(team_id: str, instance_id: str, session_id: str) -> dict:
    """删成员的一条会话。最后一条被删时成员一并移除（无会话的实例无意义）。"""
    from .sessions import delete_session  # lazy: cycle

    member = team_store.get_member(team_id, instance_id)
    if member is None:
        return {"ok": False, "error": "not found"}
    if session_id not in {*(member.get("session_ids") or []), member.get("session_id") or ""}:
        return {"ok": False, "error": "session 不属于该成员"}
    try:
        await delete_session(session_id)   # 内部含 stop 级联
    except Exception:  # noqa: BLE001
        _log.exception("team_member_session_delete_failed team=%s instance=%s session=%s", team_id, instance_id, session_id)
    updated = team_store.member_remove_session(team_id, instance_id, session_id)
    if updated is None:
        # 一条不剩：移除成员实例本身。
        team_store.remove_member(team_id, instance_id)
    _log.info("team_member_session_deleted team=%s instance=%s session=%s member_gone=%s",
              team_id, instance_id, session_id, updated is None)
    await _broadcast(team_id)
    return {"ok": True, "member": updated}


@router.post("/api/teams/{team_id}/members/{instance_id}/seen")
async def mark_seen_endpoint(team_id: str, instance_id: str, client: str = "main") -> dict:
    """用户打开了该成员工作台 → done 转 idle（§3.3 收件箱语义）。

    只有**显式聚焦**才算 seen；后台拉状态不算（herdr：*"reads do not"*）。
    """
    member = team_status.mark_seen(team_id, instance_id, client)
    if member is None:
        return {"ok": False, "error": "not found"}
    team_status.refresh_team(team_id, client)
    await _broadcast(team_id, client)
    return {"ok": True, "member": member}


# --------------------------------------------------------------------------- #
# @ 投递
# --------------------------------------------------------------------------- #

@router.post("/api/teams/{team_id}/deliver")
async def deliver_endpoint(team_id: str, req: DeliverRequest, client: str = "main") -> dict:
    """投递一条消息给成员。返回三态回执（§5.3.3）。

    ``receipt``：``delivered``（有证据）/ ``stalled``（发了但没进 working，
    **不要自动重发**）/ ``failed``（确定没发，含 blocked 原子拒绝）。
    """
    instance_id = (req.instance_id or "").strip()
    if not instance_id:
        agent_id = (req.agent_id or "").strip()
        if not agent_id:
            return {"ok": False, "receipt": "failed", "reason": "no_target",
                    "error": "需要 instance_id 或 agent_id"}
        target = team_store.member_of_agent(team_id, agent_id)
        if target is None:
            return {"ok": False, "receipt": "failed", "reason": "not_a_member",
                    "error": f"「{agent_id}」不是本团队成员"}
        instance_id = str(target.get("instance_id"))

    result = await team_deliver.deliver(team_id, instance_id, req.text,
                                        client=client, wait=req.wait)
    if result.get("receipt") != "failed":
        team_status.refresh_team(team_id, client)
        await _broadcast(team_id, client)
    return result
