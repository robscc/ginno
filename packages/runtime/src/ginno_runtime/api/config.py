"""Settings/config endpoints: general settings, model providers, agents,
MCP server config, and skills management."""

from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel

from .. import agents as agents_reg
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from ..mcp.registry import MCPRegistry
from ..server_shared import _SESSIONS, _push_global_event
from ..session_meta import _session_meta_list
from ..skills.loader import SkillLoader

router = APIRouter()


# ---- skills ----


@router.get("/api/skills")
async def list_skills(project_slug: str | None = None) -> list[dict]:
    skills = SkillLoader(project_slug=project_slug).load()
    return [
        {
            "name": s.name,
            "description": s.description,
            "trigger": s.trigger,
            "tools": s.allowed_tools,
            "builtin": s.builtin,
        }
        for s in skills
    ]


@router.get("/api/skills/{name}/body")
async def get_skill_body(name: str, project_slug: str | None = None) -> dict:
    s = SkillLoader(project_slug=project_slug).get(name)
    return {"ok": bool(s), "body": s.body if s else ""}


@router.post("/api/skills")
async def create_skill_endpoint(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    body = data.get("body") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    d = paths.global_skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    await _push_global_event("skills.changed", {})
    return {"ok": True}


@router.delete("/api/skills/{name}")
async def delete_skill_endpoint(name: str) -> dict:
    import shutil

    d = paths.global_skills_dir() / name
    if d.exists():
        shutil.rmtree(d)
        await _push_global_event("skills.changed", {})
        return {"ok": True}
    s = SkillLoader().get(name)
    if s and s.builtin:
        return {"ok": False, "error": "builtin skill cannot be deleted"}
    return {"ok": False}


@router.post("/api/skills/import-dir")
async def import_skills_dir(data: dict) -> dict:
    """Import skills from a local directory (e.g. another agent's skills folder).

    Each sub-directory containing a ``SKILL.md`` (or lowercase ``skill.md``) is
    imported as one skill; the whole sub-directory (scripts, reference docs,
    mcp-config, etc.) is copied so script-backed skills keep working. If *path*
    itself is a single skill directory, only that one is imported. Existing
    skills are skipped unless ``overwrite`` is true.

    Shares its implementation with the agent-side ``install_skills`` tool
    (:mod:`ginno_runtime.skills.installer`).
    """
    from ..skills.installer import import_skills_from_dir

    result = import_skills_from_dir(
        (data or {}).get("path", ""),
        overwrite=bool((data or {}).get("overwrite", False)),
    )
    if result.get("ok") and result.get("imported"):
        await _push_global_event("skills.changed", {})
    return result


# ---- mcp ----


@router.get("/api/mcp")
async def list_mcp() -> dict:
    if not shared._mcp:
        return {"servers": [], "tools": []}
    return {
        "servers": list(shared._mcp.ensure_loaded().keys()),
        "tools": shared._mcp.list_tools(),
    }


@router.get("/api/mcp/config")
async def get_mcp_config_endpoint() -> dict:
    p = paths.mcp_config_path()
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text() or '{"mcpServers": {}}')
    except json.JSONDecodeError:
        return {"mcpServers": {}}


@router.put("/api/mcp")
async def put_mcp_endpoint(data: dict) -> dict:
    paths.mcp_config_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}


@router.post("/api/mcp/reload")
async def reload_mcp_endpoint() -> dict:
    if shared._mcp:
        await shared._mcp.close_all()
    shared._mcp = MCPRegistry()
    shared._mcp.load()
    await shared._mcp.connect_all()
    return {"ok": True, "servers": list(shared._mcp.ensure_loaded().keys())}


# ---- settings (general) ----


@router.get("/api/settings")
async def get_settings() -> dict:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


@router.put("/api/settings")
async def put_settings(data: dict) -> dict:
    paths.settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}


# ---- providers (Settings → 模型 API) ----


def _agent_lookup(agent_id: str | None):
    return agents_reg.get_agent(agent_id) if agent_id else None


@router.get("/api/providers")
async def get_providers() -> dict:
    settings = (
        json.loads(paths.settings_path().read_text() or "{}")
        if paths.settings_path().exists()
        else {}
    )
    return {
        "default_provider": prov_mod.get_default_provider(settings),
        "providers": prov_mod.load_providers(settings),
    }


class PutProvidersRequest(BaseModel):
    providers: dict
    default_provider: str | None = None


def _refresh_session_metas() -> None:
    """Re-resolve every session meta's provider/model against current config.

    Session metas persist provider/model from creation time; the topbar label
    and rebuilt graphs (``_ensure_session``) read them, so after a provider
    config change — or on startup, to heal metas frozen by older builds — they
    must be re-resolved. Precedence mirrors ``_resolve_provider_model`` minus
    explicit request overrides: an enabled agent provider, else the enabled
    global default.
    """
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    projects_root = paths.home() / "projects"
    if not projects_root.is_dir():
        return
    for slug_dir in sorted(projects_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        slug = slug_dir.name
        metas = _session_meta_list(slug)
        if not metas:
            continue
        changed = False
        for m in metas:
            ag = _agent_lookup(m.get("agent_id"))
            provider = next(
                (
                    c
                    for c in [
                        ag.provider if ag else None,
                        prov_mod.get_default_provider(),
                    ]
                    if _enabled(c)
                ),
                None,
            ) or prov_mod.get_default_provider()
            model = (ag.model if ag and ag.model else None) or prov_mod.model_for_provider(
                providers, provider
            )
            if m.get("provider") != provider or m.get("model") != model:
                m["provider"] = provider
                m["model"] = model
                changed = True
        if changed:
            paths.session_index_path(slug).write_text(
                json.dumps(metas, indent=2, ensure_ascii=False)
            )


@router.put("/api/providers")
async def put_providers(req: PutProvidersRequest) -> dict:
    saved = prov_mod.save_providers(req.providers)
    default = req.default_provider
    if default:
        settings = (
            json.loads(paths.settings_path().read_text() or "{}")
            if paths.settings_path().exists()
            else {}
        )
        settings["default_provider"] = default
        paths.settings_path().write_text(
            json.dumps(settings, indent=2, ensure_ascii=False)
        )
    else:
        default = prov_mod.get_default_provider()
    # Evict all cached session graphs so the next WS connection rebuilds
    # with the freshly saved model/provider — otherwise existing sessions
    # keep using the LLM client that was frozen at session creation.
    _SESSIONS.clear()
    # Session metas persist provider/model from creation time; the topbar and
    # rebuilt graphs read them, so re-resolve them against the just-saved
    # config.
    _refresh_session_metas()
    return {"ok": True, "providers": saved, "default_provider": default}


@router.post("/api/providers/{provider_id}/verify")
async def verify_provider(provider_id: str) -> dict:
    return prov_mod.verify(provider_id)


@router.post("/api/providers/{provider_id}/search_probe")
def provider_search_probe(provider_id: str) -> dict:
    """User-triggered (the 测试联网 button) probe of the model's built-in web
    search. Sync so the network round-trip runs in the threadpool, not on the
    event loop."""
    from .. import providers as _prov

    return _prov.search_probe(provider_id)


# ---- agents ----


@router.get("/api/agents")
async def list_agents_endpoint() -> list[dict]:
    return [a.to_dict() for a in agents_reg.list_agents()]


@router.post("/api/agents")
async def create_agent_endpoint(data: dict) -> dict:
    try:
        agent = agents_reg.create_agent(data).to_dict()
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@router.put("/api/agents/{agent_id}")
async def update_agent_endpoint(agent_id: str, data: dict) -> dict:
    try:
        agent = agents_reg.update_agent(agent_id, data).to_dict()
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@router.delete("/api/agents/{agent_id}")
async def delete_agent_endpoint(agent_id: str) -> dict:
    ok = agents_reg.delete_agent(agent_id)
    if ok:
        await _push_global_event("agents.changed", {})
    return {"ok": ok}
