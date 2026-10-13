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
               if k in ("name", "leader_instance_id", "shared_workspace")}
    team = team_store.update_team(team_id, allowed)
    if team is None:
        return {"ok": False, "error": "not found"}
    await _broadcast(team_id)
    return {"ok": True, "team": _payload(team)}


@router.delete("/api/teams/{team_id}")
async def delete_team_endpoint(team_id: str) -> dict:
    """解散团队：停全部成员 turn + 级联删成员 session（checkpoint 不保留）。

    与 §5.6 的「归档不删」不同——这是用户显式解散，走删除语义；只想收起团队
    应该用 PATCH 改名/归档而不是 DELETE。
    """
    from .sessions import delete_session  # lazy: cycle

    team = team_store.get_team(team_id)
    if team is None:
        return {"ok": False, "error": "not found"}
    for m in team_store.list_members(team_id):
        sid = str(m.get("session_id") or "")
        if sid:
            try:
                await delete_session(sid)
            except Exception:  # noqa: BLE001 — 单个成员删不掉不该卡住解散
                _log.exception("team_member_delete_failed team=%s session=%s", team_id, sid)
    ok = team_store.delete_team(team_id)
    _log.info("team_deleted id=%s ok=%s", team_id, ok)
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
               if k in ("display_name", "role")}
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
    sid = str(member.get("session_id") or "")
    if sid:
        try:
            await delete_session(sid)   # 内部含 stop 级联
        except Exception:  # noqa: BLE001
            _log.exception("team_member_session_delete_failed session=%s", sid)
    ok = team_store.remove_member(team_id, instance_id)
    await _broadcast(team_id)
    return {"ok": ok}


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
