"""Prompt → Team (V2 P2-2).

One sentence in, an editable TeamSpec out. Nothing is written until the user
confirms the preview:

* ``POST /api/teams/generate`` — LLM drafts the spec (no side effects);
* ``POST /api/teams/from_spec`` — creates team + members + TEAM.md from a
  (possibly hand-edited) spec, then optionally delivers the first task.

The two-step shape is the point: a half-created team you cannot rename is
worse than no team at all.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ginno_runtime import jsonx as json

from .. import agents as agents_reg
from .. import providers as prov_mod
from ..models import build_model
from . import teams as teams_api

router = APIRouter()

_MAX_MEMBERS = 6

_SYSTEM = (
    "You design small agent teams. Given a user's request in Chinese or "
    "English, output ONLY a JSON object (no prose, no code fences) with:\n"
    '{"name": str, "charter_md": str, "members": [{"agent_id": str, '
    '"display_name": str, "role": "leader"|"member", "instructions": str}], '
    '"first_task": str}\n'
    "Rules: exactly one leader; 2-5 members; agent_id MUST be one of the "
    "provided ids; charter_md is a short Markdown team charter (goal + "
    "division of labour + output cadence); instructions is one paragraph "
    "describing that member's job; first_task is one concrete opening task "
    "for the team (may be empty)."
)


class GenerateRequest(BaseModel):
    prompt: str
    project_slug: str = "default"


class FromSpecRequest(BaseModel):
    name: str
    charter_md: str = ""
    project_slug: str = "default"
    members: list[dict] = []
    first_task: str = ""
    workspace: str = ""
    agent_id: str | None = None


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start = t.find("{")
    end = t.rfind("}")
    return t[start : end + 1] if start >= 0 and end > start else t


def _default_model():
    enabled = [c for c in prov_mod.load_configs() if c.get("enabled")]
    cfg = prov_mod.get_default_config() or ""
    if cfg and all(c.get("id") != cfg for c in enabled):
        cfg = ""  # the stored default points at a disabled config
    if not cfg and enabled:
        cfg = str(enabled[0].get("id") or "")
    if not cfg:
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": "no enabled model config"}
        )
    try:
        return build_model(cfg)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": str(e)}
        ) from e


def _normalize_spec(spec: dict) -> dict:
    agents = {a.id: a for a in agents_reg.list_agents()}
    fallback = next(iter(agents), "dev")
    members: list[dict] = []
    seen_leader = False
    for raw in (spec.get("members") or [])[:_MAX_MEMBERS]:
        if not isinstance(raw, dict):
            continue
        agent_id = str(raw.get("agent_id") or "").strip()
        if agent_id not in agents:
            agent_id = fallback
        role = "leader" if raw.get("role") == "leader" and not seen_leader else "member"
        if role == "leader":
            seen_leader = True
        members.append(
            {
                "agent_id": agent_id,
                "display_name": str(raw.get("display_name") or agents[agent_id].name)[:40],
                "role": role,
                "instructions": str(raw.get("instructions") or "")[:1000],
            }
        )
    if members and not seen_leader:
        members[0]["role"] = "leader"
    return {
        "name": str(spec.get("name") or "新小队")[:40],
        "charter_md": str(spec.get("charter_md") or "")[:8000],
        "members": members,
        "first_task": str(spec.get("first_task") or "")[:2000],
    }


@router.post("/api/teams/generate")
async def generate_team(req: GenerateRequest) -> dict:
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": "prompt is required"}
        )
    agents = [{"id": a.id, "name": a.name} for a in agents_reg.list_agents()]
    model = _default_model()
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = await model.ainvoke(
        [
            SystemMessage(content=_SYSTEM),
            HumanMessage(
                content=(
                    f"Available agents: {json.dumps(agents, ensure_ascii=False)}\n"
                    f"User request: {prompt}"
                )
            ),
        ]
    )
    content = getattr(resp, "content", "")
    # Usage telemetry (docs/dream-mode-design.md §8.1 #5): team generation is a
    # real completion and used to vanish from 用量统计.
    from .. import usage as usage_mod

    usage_mod.record_message(
        resp,
        source="team",
        model=str(getattr(model, "model_name", "") or getattr(model, "model", "")),
    )
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    try:
        spec = json.loads(_strip_fences(text))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "error": "model did not return a valid TeamSpec",
                "raw": text[:800],
            },
        ) from None
    return {"ok": True, "spec": _normalize_spec(spec)}


@router.post("/api/teams/from_spec")
async def team_from_spec(req: FromSpecRequest) -> dict:
    spec = _normalize_spec(
        {
            "name": req.name,
            "charter_md": req.charter_md,
            "members": req.members,
            "first_task": req.first_task,
        }
    )
    if not spec["members"]:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": "at least one member is required"},
        )
    leader = next((m for m in spec["members"] if m["role"] == "leader"), spec["members"][0])
    # 实际任命的领队 agent：调用方可用 req.agent_id 覆盖。只有当它与 spec 的
    # 领队是同一个时，才把 spec 的名字/职责给它——否则会把 A 的名字安到 B 头上。
    _leader_agent = req.agent_id or leader["agent_id"]
    _same_leader = _leader_agent == leader["agent_id"]
    created = await teams_api.create_team_endpoint(
        teams_api.CreateTeamRequest(
            name=spec["name"],
            project_slug=req.project_slug or "default",
            agent_id=_leader_agent,
            workspace=req.workspace or "",
            # 领队名/职责用 spec 的，否则 appoint_leader 会拿主会话标题
            # 「👥 队名」当领队名，且 instructions 丢失（backlog P0-6）。
            leader_display_name=leader["display_name"] if _same_leader else "",
            leader_instructions=(leader.get("instructions") or "") if _same_leader else "",
        )
    )
    if not created.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": str(created.get("error") or "create failed")},
        )
    team_id = str((created.get("team") or {}).get("id") or "")
    # 建队端点已 appoint_leader 落地领队实例——循环里必须跳过它，否则
    # 「一个团队只能有一个领队」的校验必然拒绝，member_errors 每次都含领队
    # （backlog P0-6）。用 agent_id + role 双判据认出那个已落地的领队。
    _appointed = str((created.get("team") or {}).get("leader_instance_id") or "")
    member_errors: list[str] = []
    for m in spec["members"]:
        if _appointed and m is leader and m["agent_id"] == _leader_agent:
            continue
        # 调用方用 req.agent_id 指定了**别的**领队时，spec 的领队仍带
        # role="leader"，会被「一队一领队」校验拒绝 → 该成员整个丢失。
        # 正确行为：降级为普通成员加入（不丢人，也不造双领队）。
        _role = m["role"]
        if _appointed and _role == "leader":
            _role = "member"
        resp = await teams_api.add_member_endpoint(
            team_id,
            teams_api.AddMemberRequest(
                agent_id=m["agent_id"],
                display_name=m["display_name"],
                role=_role,
                instructions=m.get("instructions") or "",
            ),
        )
        if not resp.get("ok"):
            member_errors.append(f"{m['display_name']}: {resp.get('error')}")
    if spec["charter_md"]:
        await teams_api.put_team_md(team_id, {"text": spec["charter_md"]})
    delivered = None
    if spec["first_task"]:
        # 必须带投递目标：DeliverRequest 两者皆空会直接 no_target 失败，
        # 开场任务静默丢弃（backlog P0-6）。投给领队实例。
        resp = await teams_api.deliver_endpoint(
            team_id,
            teams_api.DeliverRequest(
                text=spec["first_task"], instance_id=_appointed,
            ),
        )
        delivered = bool(resp.get("ok"))
    team = teams_api._payload(teams_api.team_store.get_team(team_id) or {})
    return {
        "ok": True,
        "team": team,
        "member_errors": member_errors,
        "first_task_delivered": delivered,
    }
