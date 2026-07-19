"""FastAPI sidecar: HTTP REST + WebSocket for the desktop UI.

Streams LangGraph events over WebSocket: token deltas, tool start/end,
permission requests (HITL), and final message boundaries.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from . import agents as agents_reg
from . import paths
from . import providers as prov_mod
from .graph import build_graph
from .hooks.dispatcher import HookDispatcher
from .models import build_model
from .mcp.registry import MCPRegistry
from .skills.loader import SkillLoader
from .todos import store as todo_store


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
    provider = (
        req.provider
        or req.model_provider
        or (agent.provider if agent else None)
        or prov_mod.get_default_provider()
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
    title = req.title or _default_title(agent_id)
    icon = req.icon or _agent_icon(agent_id)
    meta = {
        "id": session_id,
        "title": title,
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
    updated = _session_meta_patch(slug, session_id, req.model_dump())
    if s:
        for k, v in req.model_dump().items():
            if v is not None:
                s[k] = v
    return {
        "ok": True,
        "session": updated or (s and {k: v for k, v in s.items() if k != "graph"}),
    }


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
                await _run_stream(ws, graph, config, user_text, session)
            elif kind == "permission_response":
                decision = msg.get("decision", "deny")
                await _run_resume(ws, graph, config, {"decision": decision})
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
) -> None:
    """Append a HumanMessage and stream the agent loop until end or interrupt."""
    input_state = {
        "messages": [HumanMessage(content=user_text)],
        "workspace": session["workspace"],
        "project_slug": session["project_slug"],
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
        async for mode, payload in stream:
            if mode == "messages":
                chunk, msg_meta = payload
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    await ws.send_text(_ev("token.delta", {"content": content}))
                tool_calls = getattr(chunk, "tool_call_chunks", None)
                if tool_calls:
                    for tc in tool_calls:
                        if tc.get("name") and not tc.get("index") and not tc.get("args", "").strip():
                            await ws.send_text(
                                _ev("tool.start", {"name": tc["name"], "id": tc.get("id")})
                            )
            elif mode == "updates":
                # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
                for node_name, delta in (payload or {}).items():
                    if node_name == "__interrupt__":
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
                            tc_results = getattr(m, "tool_call_id", None)
                            if tc_results:
                                await ws.send_text(
                                    _ev("tool.end", {
                                        "id": tc_results,
                                        "content": getattr(m, "content", "")[:500],
                                    })
                                )
        if not saw_interrupt:
            await ws.send_text(_ev("message.end", {}))
    except Exception as e:
        await ws.send_text(_ev("error", {"message": f"{type(e).__name__}: {e}"}))


def _ev(event: str, data: dict) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False, default=str)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
