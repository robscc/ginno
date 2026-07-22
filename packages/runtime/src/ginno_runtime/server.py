"""FastAPI sidecar: HTTP REST + WebSocket for the desktop UI.

Streams LangGraph events over WebSocket: token deltas, tool start/end,
permission requests (HITL), and final message boundaries.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from . import agents as agents_reg
from . import paths
from . import providers as prov_mod
from .agents.memory import ensure_agent_memory
from .checkpointer import FileCheckpointer
from .graph import BLOCK_PREFIX, build_graph
from .tools.render_tools import RENDER_TOOL_NAMES
from .hooks.dispatcher import HookDispatcher
from .models import build_model
from .mcp.registry import MCPRegistry
from .skills.loader import SkillLoader
from .todos import store as todo_store
from . import artifacts as art_store
from . import workflows as wf_store
from .tools.artifact_tools import ARTIFACT_TOOL_NAMES
from .tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES


# Process-wide MCP registry — spawned at startup.
_mcp: MCPRegistry | None = None
_hooks: HookDispatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp, _hooks
    paths.ensure_layout()
    _mcp = MCPRegistry()
    _mcp.load()
    await _mcp.connect_all()
    _hooks = HookDispatcher.from_settings()
    todo_store.ensure_seeded()
    agents_reg.ensure_todo_tools()
    wf_store.ensure_seeded()
    yield
    if _mcp:
        await _mcp.close_all()


app = FastAPI(title="Ginno Runtime", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- serve the web UI from the sidecar (same origin as the API) ----
# In the packaged app the Tauri webview loads http://127.0.0.1:8787 directly,
# which avoids the tauri:// -> http cross-origin / mixed-content block and the
# startup race. The Next static export is bundled into the binary (web_out/) by
# PyInstaller; in dev we fall back to the repo's apps/web/out.
import sys as _sys
from pathlib import Path as _Path


def _web_out_dir() -> _Path | None:
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        p = _Path(meipass) / "web_out"
    else:
        p = _Path(__file__).resolve().parents[4] / "apps" / "web" / "out"
    return p if p.exists() else None


WEB_OUT = _web_out_dir()

if WEB_OUT is not None and (WEB_OUT / "_next").exists():
    from starlette.staticfiles import StaticFiles as _StaticFiles

    app.mount("/_next", _StaticFiles(directory=str(WEB_OUT / "_next")), name="next-static")


class CreateSessionRequest(BaseModel):
    project_slug: str
    workspace: str
    agent_id: str | None = None
    title: str | None = None
    icon: str | None = None
    provider: str | None = None
    model: str | None = None
    # legacy aliases
    model_provider: str | None = None
    model_name: str | None = None


# Session registry: holds the compiled graph + metadata (in-memory; the
# on-disk source of truth for the list is the per-slug session index).
_SESSIONS: dict[str, dict[str, Any]] = {}


def _session_meta_list(slug: str) -> list[dict]:
    p = paths.session_index_path(slug)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def _session_meta_upsert(slug: str, entry: dict) -> None:
    items = [m for m in _session_meta_list(slug) if m.get("id") != entry["id"]]
    items.insert(0, entry)
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    paths.session_index_path(slug).write_text(
        json.dumps(items, indent=2, ensure_ascii=False)
    )


def _session_meta_patch(slug: str, session_id: str, patch: dict) -> dict | None:
    items = _session_meta_list(slug)
    target = None
    for m in items:
        if m.get("id") == session_id:
            m.update({k: v for k, v in patch.items() if v is not None})
            m["updated"] = time.time()
            target = m
    if target is None:
        return None
    paths.session_index_path(slug).write_text(
        json.dumps(items, indent=2, ensure_ascii=False)
    )
    return target


def _agent_lookup(agent_id: str | None):
    return agents_reg.get_agent(agent_id) if agent_id else None


def _resolve_provider_model(req: CreateSessionRequest) -> tuple[str, str, str | None]:
    agent = _agent_lookup(req.agent_id)
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    # Prefer an *enabled* provider. The seed agents default to provider "custom",
    # which is disabled until configured; that must NOT block session creation —
    # fall through to the enabled global default so "enable a provider and use it"
    # just works without editing every agent.
    candidates = [
        req.provider,
        req.model_provider,
        agent.provider if agent else None,
        prov_mod.get_default_provider(providers),
    ]
    provider = next((c for c in candidates if _enabled(c)), None) or prov_mod.get_default_provider(
        providers
    )
    model = (
        req.model
        or req.model_name
        or (agent.model if agent and agent.model else None)
        or prov_mod.model_for_provider(providers, provider)
    )
    return provider, model, (agent.id if agent else req.agent_id)


def _default_title(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return f"{a.name} session" if a else "New Session"


def _agent_icon(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return a.icon if a else "message-square"


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@app.post("/sessions")
async def create_session(req: CreateSessionRequest) -> dict:
    provider, model_name, agent_id = _resolve_provider_model(req)
    try:
        model = build_model(provider, model_name)
    except ValueError as e:
        return {"error": str(e), "ok": False}

    mcp_tools = _mcp.all_langchain_tools() if _mcp else []
    session_id = uuid.uuid4().hex
    graph = build_graph(
        model=model,
        project_slug=req.project_slug,
        workspace=req.workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=_hooks,
    )
    ag = _agent_lookup(agent_id)
    if ag:
        ensure_agent_memory(ag.id, ag.name)
    title = req.title or _default_title(agent_id)
    title_auto = not bool(req.title)  # auto title follows the active agent
    icon = req.icon or _agent_icon(agent_id)
    meta = {
        "id": session_id,
        "title": title,
        "title_auto": title_auto,
        "icon": icon,
        "agent_id": agent_id,
        "provider": provider,
        "model": model_name,
        "workspace": req.workspace,
        "created": time.time(),
        "updated": time.time(),
    }
    _session_meta_upsert(req.project_slug, meta)
    _SESSIONS[session_id] = {
        "session_id": session_id,
        "project_slug": req.project_slug,
        "workspace": req.workspace,
        "agent_id": agent_id,
        "title": title,
        "title_auto": title_auto,
        "icon": icon,
        "model_provider": provider,
        "model_name": model_name,
        "graph": graph,
    }
    # return the meta shape (with `id`) so the frontend SessionMeta matches
    return {**meta, "ok": True}


@app.get("/sessions")
async def list_sessions(project_slug: str | None = None) -> list[dict]:
    slug = project_slug or "default"
    on_disk = _session_meta_list(slug)
    if on_disk:
        return on_disk
    return [
        {k: v for k, v in s.items() if k != "graph"}
        for s in _SESSIONS.values()
        if s.get("project_slug") == slug
    ]


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict | None:
    s = _SESSIONS.get(session_id)
    if s:
        return {k: v for k, v in s.items() if k != "graph"}
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m
    return None


class PatchSessionRequest(BaseModel):
    title: str | None = None
    icon: str | None = None
    agent_id: str | None = None


@app.patch("/sessions/{session_id}")
async def patch_session(session_id: str, req: PatchSessionRequest) -> dict:
    s = _SESSIONS.get(session_id)
    slug = s["project_slug"] if s else "default"
    patch = req.model_dump()

    # Inspect the stored meta to honour the title_auto flag: an auto-generated
    # title ("X session") follows the active agent; a manually-set title sticks.
    cur = next((m for m in _session_meta_list(slug) if m.get("id") == session_id), None)
    title_auto = (cur or {}).get("title_auto", True)
    if patch.get("title") is not None:
        title_auto = False  # explicit rename → stop auto-following
    if patch.get("agent_id") is not None and title_auto:
        ag = _agent_lookup(patch["agent_id"])
        if ag:
            patch["title"] = f"{ag.name} session"
    patch["title_auto"] = title_auto

    updated = _session_meta_patch(slug, session_id, patch)
    if s:
        for k, v in patch.items():
            if v is not None:
                s[k] = v
        s["title_auto"] = title_auto
    return {
        "ok": True,
        "session": updated or (s and {k: v for k, v in s.items() if k != "graph"}),
    }


# ---- session history (rebuild the chat UI's block layout from checkpoints) ----
def _image_block_url(b: dict) -> str | None:
    """Normalize a provider image block (OpenAI ``image_url`` / Anthropic
    ``image``) to a displayable URL (data URL for base64 sources)."""
    if b.get("type") == "image_url":
        iu = b.get("image_url") or {}
        u = iu.get("url") if isinstance(iu, dict) else None
        return u or None
    src = b.get("source") or {}
    if isinstance(src, dict):
        if src.get("type") == "url":
            return src.get("url") or None
        if src.get("data"):
            return f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
    return None


def _content_ui_blocks(content: Any) -> list[dict]:
    """Message content (str or multimodal list) -> UI text/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"kind": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                if b.strip():
                    blocks.append({"kind": "text", "text": b})
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    t = b.get("text") or ""
                    if t.strip():
                        blocks.append({"kind": "text", "text": t})
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
    return blocks


def _tool_content_str(content: Any) -> str:
    """ToolMessage.content (str or list of provider blocks) -> plain text for
    the UI tool bubble. Image parts become an ``[image]`` marker."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    parts.append(b.get("text") or "")
                elif bt in ("image", "image_url"):
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False, default=str))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


# Live WS tool outputs are capped to keep frames small; the history endpoint
# returns the full untruncated result, so expanding a bubble after reload can
# show more than what streamed live.
TOOL_OUTPUT_WS_LIMIT = 4000


def _truncate_for_ws(text: str) -> str:
    if len(text) <= TOOL_OUTPUT_WS_LIMIT:
        return text
    return text[:TOOL_OUTPUT_WS_LIMIT] + f"\n…（已截断，完整 {len(text)} 字符）"


def _ai_content_blocks(content: Any) -> list[dict]:
    """AIMessage.content (str or list of provider blocks) -> UI text/thinking/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content:
            blocks.append({"kind": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                bt = b.get("type")
                if bt == "thinking":
                    t = b.get("thinking") or b.get("text") or ""
                    if t:
                        blocks.append({"kind": "thinking", "text": t})
                elif bt == "text":
                    t = b.get("text") or ""
                    if t:
                        blocks.append({"kind": "text", "text": t})
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
            elif isinstance(b, str) and b:
                blocks.append({"kind": "text", "text": b})
    return blocks


def _run_id_in(text: str) -> str | None:
    m = re.search(r"run_id=([0-9a-f]{6,})", text or "")
    return m.group(1) if m else None


def _messages_to_ui(messages: list[Any], agent_id: str | None) -> list[dict]:
    """Convert stored LangChain messages into the chat UI's {role, blocks} shape.

    Consecutive assistant steps between two human messages are merged into a
    single assistant bubble, matching how a live turn renders (one bubble/turn).
    Special tools (render_widget/attach_ref/workflow_*) reproduce their visual
    blocks; ordinary tools fold their ToolMessage result into a tool block.
    """
    results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            results[getattr(m, "tool_call_id", None)] = _tool_content_str(getattr(m, "content", ""))

    ui: list[dict] = []
    acc: list[dict] | None = None
    acc_id: str | None = None

    def flush_assistant() -> None:
        nonlocal acc, acc_id
        if acc:
            ui.append({"id": acc_id, "role": "assistant", "agentId": agent_id, "blocks": acc})
        acc = None
        acc_id = None

    for m in messages:
        if isinstance(m, HumanMessage):
            flush_assistant()
            blocks = _content_ui_blocks(getattr(m, "content", ""))
            if blocks:
                ui.append({"id": getattr(m, "id", None), "role": "user", "blocks": blocks})
        elif isinstance(m, AIMessage):
            if acc is None:
                acc = []
                acc_id = getattr(m, "id", None)
            step = list(_ai_content_blocks(getattr(m, "content", "")))
            rk = (getattr(m, "additional_kwargs", None) or {}).get("reasoning_content")
            if rk:
                step.insert(0, {"kind": "thinking", "text": rk})
            for tc in getattr(m, "tool_calls", None) or []:
                nm = tc.get("name")
                args = tc.get("args") or {}
                tid = tc.get("id")
                res = results.get(tid, "")
                if nm == "render_widget":
                    step.append({"kind": "widget", "widgetKind": args.get("kind", "widget"), "data": args.get("data")})
                elif nm == "attach_ref":
                    step.append({
                        "kind": "ref",
                        "refKind": args.get("kind", "file"),
                        "name": args.get("name", ""),
                        "refId": args.get("ref_id", ""),
                    })
                elif nm in WORKFLOW_TOOL_NAMES:
                    rid = _run_id_in(res)
                    run = (RUN_CACHE.get(rid) if rid else None) or (wf_store.get_run(rid) if rid else None)
                    if run:
                        step.append({"kind": "workflow", "run": run})
                    else:
                        step.append({"kind": "tool", "id": tid, "name": nm, "content": res, "pending": False})
                elif nm in ARTIFACT_TOOL_NAMES or nm in RENDER_TOOL_NAMES:
                    pass  # silent / already handled above
                else:
                    step.append({"kind": "tool", "id": tid, "name": nm, "content": res, "pending": False})
            acc.extend(step)
        # ToolMessage: folded into the tool blocks above
    flush_assistant()
    return ui


def _session_slug(session_id: str) -> str | None:
    s = _SESSIONS.get(session_id)
    if s:
        return s["project_slug"]
    found = _find_meta(session_id)
    return found[1] if found else None


@app.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str) -> dict:
    """Return the persisted chat history for a session, in the UI block format."""
    slug = _session_slug(session_id)
    if not slug:
        return {"ok": True, "messages": []}
    cfg = {"configurable": {"thread_id": session_id}}
    tup = await FileCheckpointer(slug).aget_tuple(cfg)
    if not tup or not tup.checkpoint:
        return {"ok": True, "messages": []}
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    meta, _ = (_find_meta(session_id) or ({}, None))
    agent_id = meta.get("agent_id") if isinstance(meta, dict) else None
    return {"ok": True, "messages": _messages_to_ui(messages, agent_id)}


@app.get("/skills")
async def list_skills(project_slug: str | None = None) -> list[dict]:
    skills = SkillLoader(project_slug=project_slug).load()
    return [
        {
            "name": s.name,
            "description": s.description,
            "trigger": s.trigger,
            "tools": s.allowed_tools,
        }
        for s in skills
    ]


@app.get("/mcp")
async def list_mcp() -> dict:
    if not _mcp:
        return {"servers": [], "tools": []}
    return {
        "servers": list(_mcp.ensure_loaded().keys()),
        "tools": _mcp.list_tools(),
    }


@app.get("/settings")
async def get_settings() -> dict:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


# ---- providers (Settings → 模型 API) ----
@app.get("/providers")
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


@app.put("/providers")
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
    return {"ok": True, "providers": saved, "default_provider": default}


@app.post("/providers/{provider_id}/verify")
async def verify_provider(provider_id: str) -> dict:
    return prov_mod.verify(provider_id)


# ---- agents ----
@app.post("/providers/{provider_id}/search_probe")
def provider_search_probe(provider_id: str) -> dict:
    """User-triggered (the 测试联网 button) probe of the model's built-in web
    search. Sync so the network round-trip runs in the threadpool, not on the
    event loop."""
    from . import providers as _prov

    return _prov.search_probe(provider_id)


@app.get("/agents")
async def list_agents_endpoint() -> list[dict]:
    return [a.to_dict() for a in agents_reg.list_agents()]


@app.post("/agents")
async def create_agent_endpoint(data: dict) -> dict:
    try:
        return {"ok": True, "agent": agents_reg.create_agent(data).to_dict()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.put("/agents/{agent_id}")
async def update_agent_endpoint(agent_id: str, data: dict) -> dict:
    try:
        return {"ok": True, "agent": agents_reg.update_agent(agent_id, data).to_dict()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.delete("/agents/{agent_id}")
async def delete_agent_endpoint(agent_id: str) -> dict:
    return {"ok": agents_reg.delete_agent(agent_id)}


# ---- todos (global daily) ----
@app.get("/todos")
async def list_todos_endpoint() -> list[dict]:
    return todo_store.list_todos()


@app.post("/todos")
async def create_todo_endpoint(data: dict) -> dict:
    try:
        return {"ok": True, "todo": todo_store.create_todo(data)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.patch("/todos/{todo_id}")
async def update_todo_endpoint(todo_id: str, data: dict) -> dict:
    updated = todo_store.update_todo(todo_id, data)
    if updated is None:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "todo": updated}


@app.delete("/todos/{todo_id}")
async def delete_todo_endpoint(todo_id: str) -> dict:
    return {"ok": todo_store.delete_todo(todo_id)}


# ---- settings (general) ----
@app.put("/settings")
async def put_settings(data: dict) -> dict:
    paths.settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}


# ---- workflows ----
@app.get("/workflows")
async def list_workflows_endpoint() -> list[dict]:
    return wf_store.list_defs()


@app.post("/workflows")
async def create_workflow_endpoint(data: dict) -> dict:
    return {"ok": True, "workflow": wf_store.create_def(data)}


@app.put("/workflows/{wf_id}")
async def update_workflow_endpoint(wf_id: str, data: dict) -> dict:
    wf = wf_store.update_def(wf_id, data)
    return {"ok": bool(wf), "workflow": wf}


@app.delete("/workflows/{wf_id}")
async def delete_workflow_endpoint(wf_id: str) -> dict:
    return {"ok": wf_store.delete_def(wf_id)}


@app.get("/workflow_runs")
async def list_workflow_runs_endpoint() -> list[dict]:
    return wf_store.list_runs()


# ---- artifacts ----
@app.get("/artifacts")
async def list_artifacts_endpoint(project_slug: str = "default") -> list[dict]:
    return art_store.list_artifacts(project_slug)


# ---- mcp settings ----
@app.get("/mcp/config")
async def get_mcp_config_endpoint() -> dict:
    p = paths.mcp_config_path()
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text() or '{"mcpServers": {}}')
    except json.JSONDecodeError:
        return {"mcpServers": {}}


@app.put("/mcp")
async def put_mcp_endpoint(data: dict) -> dict:
    paths.mcp_config_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.post("/mcp/reload")
async def reload_mcp_endpoint() -> dict:
    global _mcp
    if _mcp:
        await _mcp.close_all()
    _mcp = MCPRegistry()
    _mcp.load()
    await _mcp.connect_all()
    return {"ok": True, "servers": list(_mcp.ensure_loaded().keys())}


# ---- skills settings ----
@app.get("/skills/{name}/body")
async def get_skill_body(name: str, project_slug: str | None = None) -> dict:
    s = SkillLoader(project_slug=project_slug).get(name)
    return {"ok": bool(s), "body": s.body if s else ""}


@app.post("/skills")
async def create_skill_endpoint(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    body = data.get("body") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    d = paths.global_skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return {"ok": True}


@app.delete("/skills/{name}")
async def delete_skill_endpoint(name: str) -> dict:
    import shutil

    d = paths.global_skills_dir() / name
    if d.exists():
        shutil.rmtree(d)
        return {"ok": True}
    return {"ok": False}


@app.post("/skills/import-dir")
async def import_skills_dir(data: dict) -> dict:
    """Import skills from a local directory (e.g. another agent's skills folder).

    Each sub-directory containing a ``SKILL.md`` (or lowercase ``skill.md``) is
    imported as one skill; the whole sub-directory (scripts, reference docs,
    mcp-config, etc.) is copied so script-backed skills keep working. If *path*
    itself is a single skill directory, only that one is imported. Existing
    skills are skipped unless ``overwrite`` is true.
    """
    import re
    import shutil
    from pathlib import Path

    from .skills.loader import _parse_skill_file

    raw = (data or {}).get("path", "")
    overwrite = bool((data or {}).get("overwrite", False))
    if not raw:
        return {"ok": False, "error": "path required"}
    src = Path(raw).expanduser().resolve()
    if not src.is_dir():
        return {"ok": False, "error": f"not a directory: {raw}"}

    def _skill_md(d: Path) -> Path | None:
        for f in d.iterdir():
            if f.is_file() and f.name.lower() == "skill.md":
                return f
        return None

    def _sanitize_name(n: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "-", (n or "").strip()).strip("-") or "skill"

    candidates = [src] if _skill_md(src) else sorted(
        c for c in src.iterdir()
        if c.is_dir() and not c.name.startswith(".") and _skill_md(c)
    )

    imported: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    dest_root = paths.global_skills_dir()
    dest_root.mkdir(parents=True, exist_ok=True)

    for c in candidates:
        smd = _skill_md(c)
        if not smd:
            continue
        parsed = _parse_skill_file(smd)
        name = _sanitize_name(parsed.name if parsed and parsed.name else c.name)
        target = dest_root / name
        if target.exists() and not overwrite:
            skipped.append({"name": name, "reason": "exists"})
            continue
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(c, target, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
            # Ginno's loader expects SKILL.md (its glob is case-sensitive), but the
            # source may use lowercase skill.md. On case-insensitive filesystems
            # (macOS APFS) a direct rename is a no-op, so go via a temp name to
            # force the case change; detect the real on-disk name via iterdir.
            actual = next(
                (f for f in target.iterdir() if f.is_file() and f.name.lower() == "skill.md"),
                None,
            )
            if actual is not None and actual.name != "SKILL.md":
                tmp = target / f".skill_md_rename_{uuid.uuid4().hex}"
                actual.rename(tmp)
                tmp.rename(target / "SKILL.md")
            imported.append({
                "name": name,
                "description": (parsed.description if parsed else "") or "",
                "from": str(c),
            })
        except Exception as e:  # noqa: BLE001
            errors.append({"name": c.name, "error": f"{type(e).__name__}: {e}"})

    return {
        "ok": True,
        "scanned": len(candidates),
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
    }


# ---- knowledge base (via MCP vault servers) ----
@app.get("/kb/servers")
async def kb_servers_endpoint() -> list[dict]:
    if not _mcp:
        return []
    return [
        {"name": n, "tools": [t.name for t in live.tools]}
        for n, live in _mcp._live.items()
    ]


def _server_roots(name: str) -> list[str]:
    """Best-effort root path(s) for a server, read from mcp.json (the filesystem
    server takes its allowed directory as a positional arg)."""
    p = paths.mcp_config_path()
    if not p.exists():
        return []
    try:
        cfg = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return []
    srv = (cfg.get("mcpServers") or {}).get(name, {}) or {}
    return [a for a in (srv.get("args") or []) if isinstance(a, str) and a.startswith("/")]


async def _kb_call_one(live, tool_name: str, args: dict) -> list[str]:
    out: list[str] = []
    if not live.session or not any(t.name == tool_name for t in live.tools):
        return out
    try:
        res = await live.session.call_tool(tool_name, args)
        for c in getattr(res, "content", []) or []:
            t = getattr(c, "text", None)
            if t:
                out.append(t)
    except Exception:
        pass
    return out


async def _kb_call(tool_name: str, args: dict) -> list[str]:
    out: list[str] = []
    if not _mcp:
        return out
    for live in _mcp._live.values():
        out.extend(await _kb_call_one(live, tool_name, args))
    return out


@app.get("/kb/search")
async def kb_search_endpoint(q: str = "") -> dict:
    if not q or not _mcp:
        return {"q": q, "results": []}
    results: list[str] = []
    for name, live in _mcp._live.items():
        for root in _server_roots(name) or [""]:
            results.extend(await _kb_call_one(live, "search_files", {"path": root, "pattern": q}))
    return {"q": q, "results": results}


@app.get("/kb/list")
async def kb_list_endpoint(path: str = "") -> dict:
    if not _mcp:
        return {"path": path, "results": []}
    results: list[str] = []
    for name, live in _mcp._live.items():
        roots = [path] if path else (_server_roots(name) or [""])
        for root in roots:
            r = await _kb_call_one(live, "list_directory", {"path": root}) or await _kb_call_one(
                live, "directory_tree", {"path": root}
            )
            results.extend(r)
    return {"path": path, "results": results}


# ---- knowledge base / LLMWiki (in-memory vault index + retrieval) ----
from .knowledge.config import load_knowledge_config as _load_kb_cfg
from .knowledge.indexer import get_indexer as _get_kb_indexer
from .knowledge.retriever import WikiRetriever as _WikiRetriever
from .knowledge import compiler as _kb_compiler
from .knowledge.association import get_engine as _get_kb_engine, reset_engines as _reset_kb_engines
from .knowledge.semantic import get_semantic_index as _get_kb_semantic, reset_semantic as _reset_kb_semantic


def _kb_not_configured(extra: dict | None = None) -> dict:
    return {"ok": False, "error": "knowledge not configured", **(extra or {})}


def _kb_indexer(cfg):
    """Shared indexer scoped to the compiled wiki: only ``wiki_dir`` is the
    searchable knowledge corpus (raw/research/loose notes stay out). An empty
    ``wiki_dir`` falls back to indexing the whole vault."""
    return _get_kb_indexer(cfg.vault_path, cfg.rescan_interval_s, include_dirs=[cfg.wiki_dir])


def _count_md(root) -> int:
    import os

    from pathlib import Path as _P

    from .knowledge.indexer import SKIP_DIRS, INDEX_EXTENSIONS

    if not root.exists():
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if _P(fn).suffix.lower() in INDEX_EXTENSIONS:
                n += 1
    return n


def _detect_wiki_layout(vault) -> dict:
    """Find a `<namespace>/Wiki` (or a root `Wiki`) layout in *vault*."""
    from pathlib import Path as _P

    def _sister(ns_dir, name):
        d = (ns_dir / name) if ns_dir else (vault / name)
        return d.relative_to(vault).as_posix() if d.is_dir() else ""

    root_wiki = vault / "Wiki"
    if root_wiki.is_dir():
        ns_dir = None
        namespace = ""
    else:
        ns_dir = None
        namespace = ""
        for child in sorted(vault.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if (child / "Wiki").is_dir():
                    ns_dir = child
                    namespace = child.name
                    break
    wiki_dir = _sister(ns_dir, "Wiki")
    return {
        "namespace": namespace,
        "wiki_dir": wiki_dir,
        "raw_dir": _sister(ns_dir, "Raw"),
        "research_dir": _sister(ns_dir, "Research"),
        "memory_dir": _sister(ns_dir, "Memory"),
        "todo_dir": _sister(ns_dir, "Todo"),
    }


@app.get("/kb/wiki/probe")
def kb_wiki_probe(path: str = "") -> dict:
    """Read-only: detect an existing LLM-Wiki layout under *path* and count pages.

    Does NOT write the vault. Used by the import UI to pre-fill config and show
    how many compiled wiki pages / raw docs a vault contains.
    """
    from pathlib import Path as _P

    if not path:
        return {"ok": False, "error": "path required"}
    vault = _P(path).expanduser().resolve()
    if not vault.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    layout = _detect_wiki_layout(vault)
    wiki_abs = (vault / layout["wiki_dir"]) if layout["wiki_dir"] else vault
    raw_abs = (vault / layout["raw_dir"]) if layout["raw_dir"] else None
    return {
        "ok": True,
        "vault_path": str(vault),
        "detected": layout,
        "wiki_pages": _count_md(wiki_abs),
        "raw_pages": _count_md(raw_abs) if raw_abs else 0,
        "has_index": (wiki_abs / "INDEX.md").is_file() if layout["wiki_dir"] else False,
        "total_md": _count_md(vault),
    }


# ---- memory summarization (P2) ----
@app.get("/memory")
async def get_memory() -> dict:
    """Return MEMORY.md content + pool count."""
    from .memory import pool_count

    p = paths.memory_index_path()
    content = p.read_text(encoding="utf-8") if p.exists() else ""
    return {"ok": True, "content": content, "pool_count": pool_count()}


@app.post("/memory/summarize")
async def post_memory_summarize(data: dict | None = None) -> dict:
    """Trigger memory summarization (pool → MEMORY.md via LLM)."""
    from .memory import summarize_pool

    provider = (data or {}).get("provider")
    return await summarize_pool(model_provider=provider)


@app.get("/kb/wiki/search")
async def kb_wiki_search(q: str = "", tag: str = "") -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"results": []})
    idx = _kb_indexer(cfg)
    entries = idx.get_entries()
    ret = _WikiRetriever(entries)
    if tag:
        results = ret.search_by_tag(tag)
    else:
        results = ret.retrieve(
            q,
            top_k=10,
            min_score=0.2,
            semantic=_get_kb_semantic(cfg, entries),
            semantic_weight=cfg.semantic_weight,
        )
    return {"ok": True, "q": q, "tag": tag, "results": [r.to_dict() for r in results]}


@app.get("/kb/wiki/list")
async def kb_wiki_list() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"pages": []})
    idx = _kb_indexer(cfg)
    pages = [
        {"title": e.title, "path": e.relative_path, "tags": e.tags, "modified": e.modified}
        for e in idx.get_entries()
    ]
    return {"ok": True, "pages": pages}


@app.get("/kb/wiki/stats")
async def kb_wiki_stats() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    entries = idx.get_entries()
    by_dir: dict[str, int] = {}
    for e in entries:
        top = e.relative_path.split("/", 1)[0] if "/" in e.relative_path else "(root)"
        by_dir[top] = by_dir.get(top, 0) + 1
    tag_counts: dict[str, int] = {}
    for e in entries:
        for t in e.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    unique_tags = sorted(tag_counts, key=lambda t: (-tag_counts[t], t))[:30]
    return {
        "ok": True,
        "vault_path": cfg.vault_path,
        "total_pages": len(entries),
        "pages_by_dir": by_dir,
        "total_links": sum(len(e.links) for e in entries),
        "total_tags": len(tag_counts),
        "unique_tags": unique_tags,
        "last_indexed": idx.last_full_scan,
    }


@app.post("/kb/wiki/index")
def kb_wiki_index() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    n = idx.scan()
    _maybe_build_semantic(cfg)
    return {"ok": True, "indexed": n, "tags": idx.get_all_tags()}


def _kb_refresh(cfg) -> None:
    """Force the shared indexer to rescan and drop the cached association graph."""
    _kb_indexer(cfg).scan()
    _reset_kb_engines()


def _maybe_build_semantic(cfg) -> None:
    """Encode wiki pages into the semantic index after a build/reindex (no-op
    unless ``use_semantic`` is on). Failures are swallowed → lexical fallback."""
    if not getattr(cfg, "use_semantic", False):
        return
    try:
        _get_kb_semantic(cfg, _kb_indexer(cfg).get_entries(), build=True)
    except Exception:  # noqa: BLE001
        pass


def _compile_to_dict(res) -> dict:
    return {
        "created": res.created,
        "updated": res.updated,
        "new_links": res.new_links,
        "discovered": res.discovered,
    }


@app.post("/kb/wiki/ingest")
def kb_wiki_ingest(data: dict) -> dict:
    """Compile a single raw file (path absolute or relative to the vault)."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    from pathlib import Path as _P

    vault = _P(cfg.vault_path).resolve()
    raw = (data or {}).get("path", "")
    p = _P(raw) if _P(raw).is_absolute() else (vault / raw).resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        return {"ok": False, "error": "path outside vault"}
    if not p.is_file():
        # compile() silently no-ops on a missing path and returned ok:True with
        # empty created/updated — callers couldn't tell failure from empty.
        return {"ok": False, "error": "file not found"}
    comp = _kb_compiler.WikiCompiler(vault, cfg.wiki_dir, cfg.raw_dir)
    res = comp.compile(p)
    comp.update_index()
    _kb_refresh(cfg)
    return {"ok": True, **_compile_to_dict(res)}


@app.post("/kb/wiki/build")
def kb_wiki_build() -> dict:
    """Compile every raw file in the vault (raw→wiki) and rebuild the index."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    from pathlib import Path as _P

    comp = _kb_compiler.WikiCompiler(_P(cfg.vault_path), cfg.wiki_dir, cfg.raw_dir)
    result = comp.build_all()
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, **result}


@app.get("/kb/wiki/related")
def kb_wiki_related(title: str = "", top_k: int = 10) -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"related": [], "clusters": []})
    idx = _kb_indexer(cfg)
    return {"ok": True, **_get_kb_engine(idx).find_related(title, top_k=top_k)}


@app.get("/kb/wiki/discover")
def kb_wiki_discover() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    return {"ok": True, **_get_kb_engine(idx).discover()}


@app.get("/kb/wiki/orphans")
def kb_wiki_orphans() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"pages": []})
    idx = _kb_indexer(cfg)
    pages = [{"title": e.title, "path": e.relative_path, "tags": e.tags} for e in idx.get_orphans()]
    return {"ok": True, "pages": pages}


@app.get("/kb/wiki/backlinks")
def kb_wiki_backlinks(title: str = "") -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"backlinks": []})
    idx = _kb_indexer(cfg)
    bl = idx.get_backlinks(title)
    return {"ok": True, "title": title, "backlinks": bl, "count": len(bl)}


@app.put("/kb/wiki/config")
async def kb_wiki_put_config(data: dict) -> dict:
    from dataclasses import fields as _fields

    from .knowledge.config import save_knowledge_config as _save_kb_cfg
    from .knowledge.types import KnowledgeConfig as _KC

    current = _load_kb_cfg()
    known = {f.name for f in _fields(_KC)}
    merged = {**current.__dict__, **{k: v for k, v in data.items() if k in known}}
    cfg = _KC(**merged)
    _save_kb_cfg(cfg)
    from .knowledge.indexer import reset_indexers as _reset_kb

    _reset_kb()  # pick up a changed vault_path on the next call
    _reset_kb_semantic()  # embeddings are keyed by vault path
    return {"ok": True, "config": merged}


def _first_agent_id() -> str | None:
    lst = agents_reg.list_agents()
    return lst[0].id if lst else None


def _find_meta(session_id: str) -> tuple[dict, str] | None:
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m, slug
    return None


def _ensure_session(session_id: str) -> dict[str, Any] | None:
    """Return the in-memory session, lazily rebuilding its graph from the
    on-disk index meta when the runtime was restarted (so sessions survive
    restarts; history is restored by the file checkpointer via thread_id)."""
    s = _SESSIONS.get(session_id)
    if s:
        return s
    found = _find_meta(session_id)
    if not found:
        return None
    meta, slug = found
    provider = meta.get("provider") or prov_mod.get_default_provider()
    model_name = meta.get("model") or prov_mod.model_for_provider(
        prov_mod.load_providers(), provider
    )
    try:
        model = build_model(provider, model_name)
    except ValueError:
        return None
    graph = build_graph(
        model=model,
        project_slug=slug,
        workspace=meta.get("workspace") or "/tmp/gw",
        mcp_tools=_mcp.all_langchain_tools() if _mcp else [],
        hook_dispatcher=_hooks,
    )
    s = {
        "session_id": session_id,
        "project_slug": slug,
        "workspace": meta.get("workspace") or "/tmp/gw",
        "agent_id": meta.get("agent_id"),
        "title": meta.get("title"),
        "icon": meta.get("icon"),
        "model_provider": provider,
        "model_name": model_name,
        "graph": graph,
    }
    _SESSIONS[session_id] = s
    return s


@app.websocket("/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    session = _ensure_session(session_id)
    if not session:
        await ws.accept()
        await ws.send_text(_ev("error", {"message": f"unknown session: {session_id}"}))
        await ws.close()
        return

    await ws.accept()
    graph = session["graph"]
    config = {"configurable": {"thread_id": session_id, "project_slug": session["project_slug"]}}

    # Re-emit any permission interrupt left pending from a previous connection:
    # the graph pauses at permission_node awaiting a resume, so a reconnect or a
    # session switch mid-permission would otherwise orphan the turn (the prompt
    # is gone and there is no way to resume). The payload mirrors the
    # __interrupt__ handling in _run_stream below, so the client's existing
    # permission.request handler applies unchanged.
    try:
        snap = await graph.aget_state(config)
        for task in getattr(snap, "tasks", []) or []:
            for intr in getattr(task, "interrupts", []) or []:
                value = getattr(intr, "value", None) or intr
                if isinstance(value, dict) and value.get("kind") == "permission_request":
                    await ws.send_text(
                        _ev(
                            "permission.request",
                            {"tool": value.get("tool"), "args": value.get("args")},
                        )
                    )
    except Exception:
        # introspecting resume state must never stop the socket from opening
        pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(_ev("error", {"message": "invalid JSON"}))
                continue

            kind = msg.get("type")
            if kind == "invoke":
                user_text = msg.get("message", "")
                user_text = _maybe_substitute_skill(user_text, session["project_slug"])
                turn_agent = (
                    msg.get("agent_id") or session.get("agent_id") or _first_agent_id()
                )
                if turn_agent != session.get("agent_id"):
                    session["agent_id"] = turn_agent
                    _session_meta_patch(
                        session["project_slug"], session_id, {"agent_id": turn_agent}
                    )
                turn_config = {
                    **config,
                    "configurable": {**config["configurable"], "agent_id": turn_agent},
                }
                await _run_stream(
                    ws,
                    graph,
                    turn_config,
                    user_text,
                    session,
                    turn_agent,
                    images=msg.get("images"),
                )
            elif kind == "permission_response":
                decision = msg.get("decision", "deny")
                # resume under the agent that was active when the interrupt fired
                resume_agent = session.get("agent_id") or _first_agent_id()
                resume_config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "agent_id": resume_agent,
                    },
                }
                await _run_resume(ws, graph, resume_config, {"decision": decision})
            elif kind == "ping":
                await ws.send_text(_ev("pong", {}))
            else:
                await ws.send_text(_ev("error", {"message": f"unknown type: {kind}"}))
    except WebSocketDisconnect:
        return


async def _run_stream(
    ws: WebSocket,
    graph,
    config: dict,
    user_text: str,
    session: dict,
    agent_id: str | None = None,
    images: list | None = None,
) -> None:
    """Append a HumanMessage and stream the agent loop until end or interrupt.

    ``images`` carries ``{"data": <base64>, "media_type": "image/png"}`` items
    from the composer; when present the HumanMessage becomes a multimodal
    content list (OpenAI-style image_url data URLs, which both ChatOpenAI and
    ChatAnthropic accept).
    """
    content: Any = user_text
    imgs = [i for i in (images or []) if isinstance(i, dict) and i.get("data")]
    if imgs:
        parts: list[dict] = []
        if user_text:
            parts.append({"type": "text", "text": user_text})
        for img in imgs:
            media = img.get("media_type") or "image/png"
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{img['data']}"}}
            )
        content = parts
    input_state = {
        "messages": [HumanMessage(content=content)],
        "workspace": session["workspace"],
        "project_slug": session["project_slug"],
        "agent_id": agent_id or session.get("agent_id") or "",
        "active_skills": [],
        "pending_tool_calls": [],
    }
    await _stream_graph(ws, graph, config, input_state=input_state)


def _maybe_substitute_skill(user_text: str, project_slug: str) -> str:
    """If user_text starts with `/<skill-name>`, replace with SKILL.md body +
    the trailing question. Otherwise return as-is."""
    stripped = user_text.lstrip()
    if not stripped.startswith("/"):
        return user_text
    # Parse `/skill-name rest of message`
    rest = stripped[1:]
    parts = rest.split(maxsplit=1)
    if not parts or not parts[0]:
        return user_text
    skill_name = parts[0]
    tail = parts[1] if len(parts) > 1 else ""
    skill = SkillLoader(project_slug=project_slug).get(skill_name)
    if not skill or not skill.body:
        return user_text  # unknown skill — leave the message alone
    blocks = [
        f"<skill name=\"{skill_name}\">",
        skill.body.strip(),
        "</skill>",
    ]
    if tail:
        blocks.append(f"\n\nUser request: {tail}")
    else:
        blocks.append("\n\n(Follow the skill instructions above.)")
    return "\n".join(blocks)


async def _run_resume(ws: WebSocket, graph, config: dict, resume_value: dict) -> None:
    """Resume the graph from a pending interrupt (e.g. permission ask)."""
    await _stream_graph(ws, graph, config, command=Command(resume=resume_value))


async def _stream_graph(
    ws: WebSocket,
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """Drive the graph and emit token / tool / permission events."""
    try:
        if command is not None:
            stream = graph.astream(command, config=config, stream_mode=["messages", "updates"])
        else:
            stream = graph.astream(input_state, config=config, stream_mode=["messages", "updates"])

        saw_interrupt = False
        special_ids: dict[str, str] = {}  # tool_call id -> special tool name (no bubble)
        slug = (config.get("configurable") or {}).get("project_slug", "default")
        session_id = (config.get("configurable") or {}).get("thread_id", "")
        agent_id = (config.get("configurable") or {}).get("agent_id", "")
        turn_text: list[str] = []  # accumulate assistant text for memory capture
        # Fresh turn (not a permission resume): announce the resolved agent so the
        # UI can label the assistant bubble authoritatively (never the generic
        # "Agent" fallback).
        if command is None:
            _aid = (config.get("configurable") or {}).get("agent_id")
            _ag = agents_reg.get_agent(_aid) if _aid else None
            await ws.send_text(
                _ev("turn.start", {"agent_id": _aid or "", "name": _ag.name if _ag else "Agent"})
            )
        async for mode, payload in stream:
            if mode == "messages":
                chunk, msg_meta = payload
                # Only AI message chunks carry streaming text / thinking /
                # tool-call chunks. ToolMessage chunks (the result emitted by
                # the tools node, type "tool") must NOT be streamed as
                # token.delta — that would leak the tool output into the
                # assistant's text bubble (and into the memory-capture buffer).
                # Tool results reach the UI via the `updates` mode -> tool.end.
                # Streamed AI chunks report type "AIMessageChunk"; a final
                # non-streamed AIMessage reports "ai" — allow both.
                if getattr(chunk, "type", None) not in ("ai", "AIMessageChunk"):
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    for b in content:
                        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                        if btype == "thinking":
                            txt = b.get("thinking") or b.get("text") or ""
                            if txt:
                                await ws.send_text(_ev("thinking.delta", {"content": txt}))
                        elif btype == "text":
                            txt = b.get("text") or ""
                            if txt:
                                turn_text.append(txt)
                                await ws.send_text(_ev("token.delta", {"content": txt}))
                elif isinstance(content, str) and content:
                    turn_text.append(content)
                    await ws.send_text(_ev("token.delta", {"content": content}))
                rk = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
                if rk:
                    await ws.send_text(_ev("thinking.delta", {"content": rk}))
                tool_calls = getattr(chunk, "tool_call_chunks", None)
                if tool_calls:
                    for tc in tool_calls:
                        if tc.get("name") and not tc.get("index") and not tc.get("args", "").strip():
                            if (
                                tc["name"] in RENDER_TOOL_NAMES
                                or tc["name"] in WORKFLOW_TOOL_NAMES
                                or tc["name"] in ARTIFACT_TOOL_NAMES
                            ):
                                special_ids[tc.get("id")] = tc["name"]
                                continue  # surfaced as widget/ref/workflow block, not a tool bubble
                            await ws.send_text(
                                _ev("tool.start", {"name": tc["name"], "id": tc.get("id")})
                            )
            elif mode == "updates":
                # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
                for node_name, delta in (payload or {}).items():
                    if node_name == "agent":
                        for m in (delta or {}).get("messages", []):
                            for tc in getattr(m, "tool_calls", []) or []:
                                nm = tc.get("name")
                                args = tc.get("args") or {}
                                if (
                                    nm in RENDER_TOOL_NAMES
                                    or nm in WORKFLOW_TOOL_NAMES
                                    or nm in ARTIFACT_TOOL_NAMES
                                ):
                                    special_ids[tc.get("id")] = nm
                                if nm == "render_widget":
                                    await ws.send_text(
                                        _ev("widget.emit", {
                                            "kind": args.get("kind", "widget"),
                                            "data": args.get("data"),
                                        })
                                    )
                                elif nm == "attach_ref":
                                    kind = args.get("kind", "file")
                                    await ws.send_text(
                                        _ev("ref.emit", {
                                            "kind": kind,
                                            "name": args.get("name", ""),
                                            "ref_id": args.get("ref_id", ""),
                                        })
                                    )
                                    if kind in ("file", "doc", "workflow", "link"):
                                        art_store.add_artifact(
                                            slug, kind, args.get("name", ""), args.get("ref_id", "")
                                        )
                                elif nm == "artifact_register":
                                    art_store.add_artifact(
                                        slug,
                                        args.get("kind", "file"),
                                        args.get("name", ""),
                                        args.get("ref", ""),
                                    )
                    elif node_name == "__interrupt__":
                        items = delta if isinstance(delta, (list, tuple)) else [delta]
                        for intr in items:
                            value = getattr(intr, "value", None) or intr
                            if isinstance(value, dict) and value.get("kind") == "permission_request":
                                saw_interrupt = True
                                await ws.send_text(
                                    _ev("permission.request", {
                                        "tool": value.get("tool"),
                                        "args": value.get("args"),
                                    })
                                )
                    elif node_name == "tools":
                        msgs = (delta or {}).get("messages", [])
                        for m in msgs:
                            tc_id = getattr(m, "tool_call_id", None)
                            raw = getattr(m, "content", "") or ""
                            nm = special_ids.get(tc_id)
                            if nm in WORKFLOW_TOOL_NAMES:
                                mm = re.search(r"run_id=([0-9a-f]{6,})", raw)
                                rid = mm.group(1) if mm else None
                                run = (RUN_CACHE.get(rid) if rid else None) or (
                                    wf_store.get_run(rid) if rid else None
                                )
                                if run:
                                    await ws.send_text(_ev("workflow.emit", {"run": run}))
                                # no ordinary tool bubble for workflow tools
                            elif nm in RENDER_TOOL_NAMES or nm in ARTIFACT_TOOL_NAMES:
                                pass  # widget/ref emitted at agent-update; no bubble
                            elif tc_id:
                                await ws.send_text(
                                    _ev("tool.end", {"id": tc_id, "content": _truncate_for_ws(_tool_content_str(raw))})
                                )
                    elif node_name == "permission":
                        # resolve "running" tool bubbles that were denied by
                        # tools_allow / hooks / policy / user (not streamed)
                        for m in (delta or {}).get("messages", []):
                            c = getattr(m, "content", "")
                            if isinstance(c, str) and c.startswith(BLOCK_PREFIX):
                                rest = c[len(BLOCK_PREFIX):]
                                name, _, reason = rest.partition("]")
                                await ws.send_text(
                                    _ev("tool.end", {
                                        "name": name.strip(),
                                        "content": reason.strip() or c,
                                    })
                                )
        # refresh the right-panel TODO list after every turn (the agent may
        # have mutated it via the todo_* tools); the checkbox path is optimistic
        # and doesn't need this.
        await ws.send_text(_ev("todos.changed", {}))
        await ws.send_text(_ev("workflows.changed", {}))
        await ws.send_text(_ev("artifacts.changed", {}))
        if not saw_interrupt:
            # Capture sanitized assistant text for memory summarization (P2)
            if turn_text:
                from .memory import append_to_pool

                append_to_pool(session_id, agent_id, "".join(turn_text))
            await ws.send_text(_ev("message.end", {}))
    except Exception as e:
        await ws.send_text(_ev("error", {"message": f"{type(e).__name__}: {e}"}))


def _ev(event: str, data: dict) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False, default=str)


@app.get("/{full_path:path}")
async def _serve_web(full_path: str):
    """Serve the bundled Next static export (same origin as the API)."""
    from fastapi.responses import FileResponse, HTMLResponse

    if WEB_OUT is None:
        return HTMLResponse("frontend not bundled", status_code=404)
    p = full_path.strip("/")
    if p == "":
        return FileResponse(WEB_OUT / "index.html")
    cand = WEB_OUT / (p + ".html")
    if cand.exists():
        return FileResponse(cand)
    idx = WEB_OUT / p / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return FileResponse(WEB_OUT / "index.html")  # SPA fallback


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
