"""FastAPI sidecar: HTTP REST + WebSocket for the desktop UI.

P0 endpoints: health, sessions (create/list/get), invoke (streaming via WS).

The desktop shell (Tauri) spawns this sidecar on startup and talks to it
over localhost. WebSocket streams LangGraph events (token deltas, tool
events, permission requests, hook events).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import paths
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


class InvokeRequest(BaseModel):
    session_id: str
    message: str


# in-memory session registry (P0; the file checkpointer persists state on disk)
_SESSIONS: dict[str, dict[str, Any]] = {}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@app.post("/sessions")
async def create_session(req: CreateSessionRequest) -> dict:
    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = {
        "session_id": session_id,
        "project_slug": req.project_slug,
        "workspace": req.workspace,
        "model_provider": req.model_provider,
        "model_name": req.model_name,
    }
    return _SESSIONS[session_id]


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return list(_SESSIONS.values())


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict | None:
    return _SESSIONS.get(session_id)


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


@app.websocket("/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    """P1: stream LangGraph events. P0: echo + close with a clear message."""
    await ws.accept()
    try:
        await ws.send_text(json.dumps({"event": "info", "message": "ws scaffold; streaming lands in P1"}))
        while True:
            msg = await ws.receive_text()
            await ws.send_text(json.dumps({"event": "echo", "message": msg}))
    except WebSocketDisconnect:
        return


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
