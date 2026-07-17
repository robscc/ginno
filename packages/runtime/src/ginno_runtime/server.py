"""FastAPI sidecar: HTTP REST + WebSocket for the desktop UI.

Streams LangGraph events over WebSocket: token deltas, tool start/end,
permission requests (HITL), and final message boundaries.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from . import paths
from .graph import build_graph
from .models import build_model
from .skills.loader import SkillLoader


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_layout()
    yield


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
    model_provider: str = "anthropic"
    model_name: str = "claude-sonnet-4-6"


# Session registry: holds the compiled graph + metadata.
_SESSIONS: dict[str, dict[str, Any]] = {}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@app.post("/sessions")
async def create_session(req: CreateSessionRequest) -> dict:
    try:
        model = build_model(req.model_provider, req.model_name)
    except ValueError as e:
        return {"error": str(e), "ok": False}

    session_id = uuid.uuid4().hex
    graph = build_graph(
        model=model,
        project_slug=req.project_slug,
        workspace=req.workspace,
    )
    _SESSIONS[session_id] = {
        "session_id": session_id,
        "project_slug": req.project_slug,
        "workspace": req.workspace,
        "model_provider": req.model_provider,
        "model_name": req.model_name,
        "graph": graph,
    }
    info = {k: v for k, v in _SESSIONS[session_id].items() if k != "graph"}
    info["ok"] = True
    return info


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return [
        {k: v for k, v in s.items() if k != "graph"}
        for s in _SESSIONS.values()
    ]


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict | None:
    s = _SESSIONS.get(session_id)
    if not s:
        return None
    return {k: v for k, v in s.items() if k != "graph"}


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
    from .mcp.registry import MCPRegistry

    return {"servers": list(MCPRegistry().load().keys())}


@app.get("/settings")
async def get_settings() -> dict:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


@app.websocket("/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    session = _SESSIONS.get(session_id)
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
                await _run_stream(ws, graph, config, msg.get("message", ""), session)
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

        async for mode, payload, metadata in stream:
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
                # payload is {node_name: state_delta}
                for node_name, delta in (payload or {}).items():
                    if node_name == "tools":
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
        # stream ended — check for pending interrupt (permission ask)
        state = await graph.aget_state(config)
        if state and state.tasks:
            for task in state.tasks:
                interrupts = getattr(task, "interrupts", None) or []
                for intr in interrupts:
                    payload = getattr(intr, "value", None) or {}
                    if isinstance(payload, dict) and payload.get("kind") == "permission_request":
                        await ws.send_text(
                            _ev("permission.request", {
                                "tool": payload.get("tool"),
                                "args": payload.get("args"),
                            })
                        )
                        return
        await ws.send_text(_ev("message.end", {}))
    except Exception as e:
        await ws.send_text(_ev("error", {"message": f"{type(e).__name__}: {e}"}))


def _ev(event: str, data: dict) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False, default=str)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
