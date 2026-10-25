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
    created = await teams_api.create_team_endpoint(
        teams_api.CreateTeamRequest(
            name=spec["name"],
            project_slug=req.project_slug or "default",
            agent_id=req.agent_id or leader["agent_id"],
            workspace=req.workspace or "",
        )
    )
    if not created.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": str(created.get("error") or "create failed")},
        )
    team_id = str((created.get("team") or {}).get("id") or "")
    member_errors: list[str] = []
    for m in spec["members"]:
        resp = await teams_api.add_member_endpoint(
            team_id,
            teams_api.AddMemberRequest(
                agent_id=m["agent_id"],
                display_name=m["display_name"],
                role=m["role"],
            ),
        )
        if not resp.get("ok"):
            member_errors.append(f"{m['display_name']}: {resp.get('error')}")
    if spec["charter_md"]:
        await teams_api.put_team_md(team_id, {"text": spec["charter_md"]})
    delivered = None
    if spec["first_task"]:
        resp = await teams_api.deliver_endpoint(
            team_id, teams_api.DeliverRequest(text=spec["first_task"])
        )
        delivered = bool(resp.get("ok"))
    team = teams_api._payload(teams_api.team_store.get_team(team_id) or {})
    return {
        "ok": True,
        "team": team,
        "member_errors": member_errors,
        "first_task_delivered": delivered,
    }
