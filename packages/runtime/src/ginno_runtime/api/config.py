"""Settings/config endpoints: general settings, model providers, agents,
MCP server config, and skills management."""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import agents as agents_reg
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from ..agents.registry import AgentModelBindingError
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
        return {"servers": [], "tools": [], "failed": []}
    reg = shared._mcp
    # Lazy healing (2026-08-10 incident): a startup-time DNS/network blip
    # used to leave MCP dead until a manual reload or app restart. The UI
    # polls this endpoint, so opportunistically re-attempt failed servers
    # here — cooldown + busy-guard live inside retry_failed(); the task is
    # fire-and-forget so a hung server can never stall the settings page.
    if reg.has_pending_failures():
        shared.spawn_bg(reg.retry_failed())
    return {
        "servers": list(reg.ensure_loaded().keys()),
        "tools": reg.list_tools(),
        "failed": reg.failed_servers,
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
    prev_use_proxy = prov_mod.use_system_proxy()  # reads the still-on-disk value
    paths.settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    new_use_proxy = prov_mod.use_system_proxy(data)
    if new_use_proxy != prev_use_proxy:
        # httpx clients freeze their proxy map at init — swap the lookup,
        # then drop every cache still holding clients built under the old
        # mode (apply_system_proxy also clears langchain_anthropic's shared
        # lru_cache'd clients).
        prov_mod.apply_system_proxy(new_use_proxy)
        _SESSIONS.clear()
    return {"ok": True}


# ---- model configs (Settings → 模型 API, v2) ----


def _agent_lookup(agent_id: str | None):
    return agents_reg.get_agent(agent_id) if agent_id else None


def _read_settings_file() -> dict:
    return (
        json.loads(paths.settings_path().read_text() or "{}")
        if paths.settings_path().exists()
        else {}
    )


@router.get("/api/model_configs")
async def get_model_configs() -> dict:
    settings = _read_settings_file()
    return {
        "configs": prov_mod.load_configs(settings),
        "default_config": prov_mod.get_default_config(settings),
    }


class PutModelConfigsRequest(BaseModel):
    configs: list
    default_config: str | None = None


def _config_references(config_id: str) -> dict:
    """Who still points at a config: agents (by provider id) + default flag."""
    settings = _read_settings_file()
    current_default = settings.get("default_config") or settings.get("default_provider")
    agent_refs = [
        {"id": a.id, "name": a.name}
        for a in agents_reg.list_agents()
        if a.provider == config_id
    ]
    return {
        "agents": agent_refs,
        "is_default": current_default == config_id,
    }


def _guard_config_removal(stored_ids: set[str], new_ids: set[str], new_default: str | None) -> None:
    """Deletion guard (decision: 删除被引用配置 → 400 带引用清单).

    A config that disappears in a full-array PUT may not be referenced by any
    agent, nor be the current default unless the request also moves the
    default elsewhere.
    """
    blocked: dict[str, dict] = {}
    for rid in sorted(stored_ids - new_ids):
        refs = _config_references(rid)
        if refs["agents"] or (refs["is_default"] and not new_default):
            blocked[rid] = refs
    if blocked:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "以下配置仍被引用，无法删除: "
                + ", ".join(blocked.keys()),
                "references": blocked,
            },
        )
    return None


@router.put("/api/model_configs")
async def put_model_configs(req: PutModelConfigsRequest):
    try:
        normalized = prov_mod.validate_configs(req.configs)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    new_ids = {c["id"] for c in normalized}
    default = req.default_config
    if default is not None and default not in new_ids:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"default_config {default!r} 不在 configs 内"},
        )
    blocked = _guard_config_removal(
        {c["id"] for c in prov_mod.load_configs()}, new_ids, default
    )
    if isinstance(blocked, JSONResponse):
        return blocked
    saved = prov_mod.save_configs(normalized, default)
    # Same eviction semantics as the legacy PUT /api/providers: drop cached
    # session graphs and re-resolve persisted metas against the new config.
    _SESSIONS.clear()
    _refresh_session_metas()
    return {
        "ok": True,
        "configs": saved,
        "default_config": prov_mod.get_default_config(),
    }


@router.post("/api/model_configs/verify")
async def verify_model_config_endpoint(cfg: dict) -> dict:
    """Verify a config DRAFT; on success (decision Q4) the draft is saved
    immediately (upsert by id, ``verified_at`` stamped) and returned. On
    failure nothing is written and ``{ok: false, error}`` comes back with 200
    so the UI can show the yellow banner."""
    result = prov_mod.verify_config_draft(cfg)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error"),
            "latency_ms": result.get("latency_ms"),
        }
    saved_cfg = prov_mod.normalize_config(cfg)
    saved_cfg["verified_at"] = int(time.time())
    saved_cfg["last_error"] = None
    if not saved_cfg.get("id"):
        saved_cfg["id"] = f"prov_{uuid.uuid4().hex[:8]}"
    merged = {c["id"]: c for c in prov_mod.load_configs()}
    merged[saved_cfg["id"]] = saved_cfg
    prov_mod.save_configs(list(merged.values()))
    return {
        "ok": True,
        "config": saved_cfg,
        "latency_ms": result.get("latency_ms"),
    }


@router.post("/api/model_configs/list_models")
def list_model_config_models(cfg: dict) -> dict:
    """Fetch a config DRAFT's model catalogue from its provider (「从 API 拉取
    模型」). Nothing is saved — id not required. Sync so the network round-trip
    runs in the threadpool; HTTP 200 always, ok:false carries the error, same
    convention as the verify endpoint."""
    return prov_mod.list_models_draft(cfg)


@router.delete("/api/model_configs/{config_id}")
async def delete_model_config(config_id: str):
    stored = {c["id"]: c for c in prov_mod.load_configs()}
    if config_id not in stored:
        return JSONResponse(
            status_code=404, content={"ok": False, "error": f"unknown config: {config_id}"}
        )
    refs = _config_references(config_id)
    if refs["agents"] or refs["is_default"]:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": f"配置 {config_id} 仍被引用，无法删除",
                # same shape as the PUT removal guard: keyed by config id
                "references": {config_id: refs},
            },
        )
    remaining = [c for c in stored.values() if c["id"] != config_id]
    settings = _read_settings_file()
    was_default = (settings.get("default_config") or settings.get("default_provider")) == config_id
    new_default = None
    if was_default:
        # design §2.5: default moves to the first enabled config (or clears to
        # "" so the legacy default_provider fallback takes over)
        new_default = next((c["id"] for c in remaining if c.get("enabled")), "")
    prov_mod.save_configs(remaining, new_default)
    _SESSIONS.clear()
    _refresh_session_metas()
    return {"ok": True, "default_config": prov_mod.get_default_config()}


# ---- providers (Settings → 模型 API, legacy read-only-compat view) ----


def _config_to_legacy(cfg: dict) -> dict:
    """Project a v2 config into the legacy slot-dict shape the pre-v2
    frontend reads: no ``id`` key (the dict key IS the id), and the custom
    slot keeps its historical ``model`` field name alongside ``default_model``."""
    out = {k: v for k, v in cfg.items() if k != "id"}
    out["default_model"] = cfg.get("default_model") or ""
    out.setdefault("model", out["default_model"])
    return out


@router.get("/api/providers")
async def get_providers() -> dict:
    settings = _read_settings_file()
    configs = prov_mod.load_configs(settings)
    return {
        "default_provider": prov_mod.get_default_config(settings),
        "providers": {cfg["id"]: _config_to_legacy(cfg) for cfg in configs},
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
    # Forward to the v2 store: the dict payload is converted to configs and
    # merged by id (configs not expressible in the legacy dict form survive);
    # the default is written to the NEW key only (default_config).
    saved = prov_mod.save_providers(req.providers)
    default = req.default_provider
    if default:
        settings = _read_settings_file()
        settings["default_config"] = default
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
    except AgentModelBindingError as e:
        # decision Q3: hard constraint violations are a real 400
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@router.put("/api/agents/{agent_id}")
async def update_agent_endpoint(agent_id: str, data: dict) -> dict:
    try:
        agent = agents_reg.update_agent(agent_id, data).to_dict()
    except AgentModelBindingError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
