"""Shared pytest fixtures for the Ginno runtime test suite.

Isolation model
---------------
Every store, the file checkpointer, and all settings read through
``ginno_runtime.paths.home()``, which honors the ``$GINNO_HOME`` env var at
*call time*. The autouse ``isolated_home`` fixture points ``$GINNO_HOME`` at a
fresh ``tmp_path`` per test and clears the server's process-wide globals, so no
test touches the real ``~/.ginno`` and none leaks state into another.

Two client styles
-----------------
* API + E2E tests are **sync** and use ``fastapi.testclient.TestClient`` (as a
  context manager, so the FastAPI lifespan runs and seeds the home).
* A few unit tests that drive ``build_graph`` directly are **async**
  (``asyncio_mode = "auto"``); never call ``TestClient`` from inside those.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from ginno_runtime import paths, server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call
from langchain_core.messages import AIMessage


def as_fake_model(model_or_scripts: Any) -> ScriptedChatModel:
    """Normalize a test's LLM spec into a ScriptedChatModel.

    Accepts a ScriptedChatModel (as-is), a list of AIMessage turns, or a single
    AIMessage — so tests can write ``create_session([script(text="hi")])``.
    """
    if isinstance(model_or_scripts, ScriptedChatModel):
        return model_or_scripts
    if isinstance(model_or_scripts, AIMessage):
        return ScriptedChatModel(scripts=[model_or_scripts])
    if isinstance(model_or_scripts, list):
        return ScriptedChatModel(scripts=list(model_or_scripts))
    return model_or_scripts  # already some other model; pass through


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $GINNO_HOME at a fresh temp dir and reset server globals per test."""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    # Process-wide state that the lifespan does NOT reset between tests.
    server._SESSIONS.clear()
    server._USAGE_BY_SESSION.clear()
    server._mcp = None
    server._hooks = None
    # Keep the default Playwright MCP out of unrelated tests (it would spawn a
    # headless browser on every server start). ensure_layout only re-seeds the
    # default when mcp.json is missing/empty, so a non-empty stub opts us out.
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    return tmp_path


@pytest.fixture
def seeded_home(isolated_home: Path) -> Path:
    """Isolated home with the standard layout + seed defaults written to disk."""
    paths.ensure_layout()
    return isolated_home


# --------------------------------------------------------------------------- #
# Fake LLM
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_model_factory() -> Callable[..., ScriptedChatModel]:
    """Build a ScriptedChatModel from scripted turns (text and/or tool calls)."""

    def _make(scripts: list) -> ScriptedChatModel:
        return ScriptedChatModel(scripts=scripts)

    return _make


# --------------------------------------------------------------------------- #
# HTTP client + build_model patch
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_build_model(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], Any]:
    """Patch server.build_model so create_session/ensure_session use the fake."""

    def _patch(model: Any) -> Any:
        monkeypatch.setattr(server, "build_model", lambda *a, **k: model)
        return model

    return _patch


@pytest.fixture
def client(isolated_home: Path) -> TestClient:
    """FastAPI TestClient with lifespan run (seeds the isolated home)."""
    with TestClient(server.app) as c:
        # Tests default privileged mode OFF so permission ask/deny is observable;
        # production defaults to ON. permission_node reads this live from settings.
        sp = isolated_home / "settings.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            s["bypass_permissions"] = False
            sp.write_text(json.dumps(s))
        yield c


# --------------------------------------------------------------------------- #
# Session + WebSocket helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def create_session(client: TestClient, patch_build_model: Callable) -> Callable:
    """Patch the model and POST /sessions; returns the new session id.

    Signature: create_session(model, *, agent_id="dev", slug="default",
    workspace=None, title=None) -> session_id
    """

    def _create(
        model: Any,
        *,
        agent_id: str | None = "dev",
        slug: str = "default",
        workspace: str | None = None,
        title: str | None = None,
    ) -> str:
        patch_build_model(as_fake_model(model))
        ws = workspace or str(Path(os.environ["GINNO_HOME"]) / "ws")
        Path(ws).mkdir(parents=True, exist_ok=True)
        body: dict[str, Any] = {"project_slug": slug, "workspace": ws}
        if agent_id is not None:
            body["agent_id"] = agent_id
        if title is not None:
            body["title"] = title
        r = client.post("/api/sessions", json=body)
        data = r.json()
        assert r.status_code == 200 and data.get("ok") is not False, data
        return data["id"]

    return _create


class WSConversation:
    """A thin sync wrapper over one WebSocket session connection.

    Usage::

        with ws_conv(session_id) as conv:
            conv.invoke("hello")
            events = conv.recv_until("message.end")
    """

    def __init__(self, client: TestClient, session_id: str) -> None:
        self._client = client
        self.session_id = session_id
        self._cm = None
        self.ws = None

    def __enter__(self) -> "WSConversation":
        self._cm = self._client.websocket_connect(f"/api/ws/sessions/{self.session_id}")
        self.ws = self._cm.__enter__()
        return self

    def __exit__(self, *exc: Any) -> bool | None:
        return self._cm.__exit__(*exc)

    def send(self, obj: dict) -> None:
        self.ws.send_json(obj)

    def recv(self) -> dict:
        return self.ws.receive_json()

    def invoke(
        self,
        message: str,
        agent_id: str | None = None,
        *,
        mentions: list | None = None,
        files: list | None = None,
        images: list | None = None,
    ) -> None:
        payload: dict[str, Any] = {"type": "invoke", "message": message}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if mentions is not None:
            payload["mentions"] = mentions
        if files is not None:
            payload["files"] = files
        if images is not None:
            payload["images"] = images
        self.send(payload)

    def respond_permission(self, decision: str) -> None:
        self.send({"type": "permission_response", "decision": decision})

    def recv_until(self, *terminal: str) -> list[dict]:
        """Receive events until one of the terminal event names is seen."""
        terminal_set = set(terminal)
        events: list[dict] = []
        while True:
            ev = self.recv()
            events.append(ev)
            if ev.get("event") in terminal_set:
                break
        return events


@pytest.fixture
def ws_conv(client: TestClient) -> Callable[[str], WSConversation]:
    """Factory: ws_conv(session_id) -> WSConversation bound to the test client."""

    def _make(session_id: str) -> WSConversation:
        return WSConversation(client, session_id)

    return _make


# --------------------------------------------------------------------------- #
# Knowledge / LLMWiki fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def kb_vault(isolated_home: Path) -> Path:
    """Create a small Obsidian vault with sample entries and enable knowledge.

    Returns the vault path. Resets the shared indexer cache before and after so
    tests never share index state.
    """
    from ginno_runtime.knowledge.indexer import reset_indexers

    vault = isolated_home / "vault"
    (vault / "Ginno" / "Wiki" / "concepts").mkdir(parents=True)
    (vault / "Ginno" / "Wiki" / "concepts" / "permission.md").write_text(
        "---\ntitle: LangGraph 权限节点\ntags: [arch, permission]\n---\n"
        "# 权限节点\n\n权限节点按 deny→ask→allow 顺序匹配，ask 会触发 interrupt 等待用户确认。\n",
        encoding="utf-8",
    )
    (vault / "Ginno" / "Wiki" / "concepts" / "checkpointer.md").write_text(
        "---\ntitle: 文件 Checkpointer\ntags: [arch, persistence]\n---\n"
        "# Checkpointer\n\n每个 session 一个 JSON 文件，原子写入，支持时间旅行恢复。"
        "相关 [[LangGraph 权限节点]]。\n",
        encoding="utf-8",
    )
    (vault / "Ginno" / "Wiki" / "concepts" / "cooking.md").write_text(
        "---\ntitle: 红烧肉做法\ntags: [cooking]\n---\n# 红烧肉\n\n五花肉焯水加冰糖酱油。\n",
        encoding="utf-8",
    )

    settings = (
        json.loads(paths.settings_path().read_text()) if paths.settings_path().exists() else {}
    )
    settings["knowledge"] = {
        "enabled": True,
        "vault_path": str(vault),
        "inject_top_k": 5,
        "inject_min_score": 0.3,
    }
    paths.settings_path().write_text(json.dumps(settings))
    reset_indexers()
    yield vault
    reset_indexers()


# --------------------------------------------------------------------------- #
# Small event helpers (importable in tests via `from conftest import ...`)
# --------------------------------------------------------------------------- #
def event_names(events: list[dict]) -> list[str]:
    return [e.get("event") for e in events]


def events_of(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


# Re-export builders so tests can `from conftest import script, script_tool_call`.
__all__ = [
    "script",
    "script_tool_call",
    "event_names",
    "events_of",
    "WSConversation",
]
