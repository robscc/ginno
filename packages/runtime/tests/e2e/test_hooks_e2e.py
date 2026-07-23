"""WebSocket E2E: a PreToolUse hook blocks a tool before the permission gate.

The hook dispatcher is built from settings.json during the FastAPI lifespan, so
the hook must be written *before* the TestClient starts — hence the dedicated
``hook_client`` fixture below (the shared ``client`` fixture starts lifespan
immediately on an empty home).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import WSConversation, event_names, events_of
from ginno_runtime import paths, server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

pytestmark = pytest.mark.e2e


@pytest.fixture
def hook_client(isolated_home):
    """A TestClient whose home has a PreToolUse block hook on `bash`."""
    paths.ensure_layout()
    hook_script = isolated_home / "block_bash.py"
    hook_script.write_text(
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'block': True, 'reason': 'bash is disabled'}))\n"
    )
    settings = json.loads(paths.settings_path().read_text())
    settings["hooks"] = {
        "PreToolUse": [{"matcher": "bash", "command": f"{sys.executable} {hook_script}"}]
    }
    paths.settings_path().write_text(json.dumps(settings))
    with TestClient(server.app) as c:
        yield c


def test_prettooluse_hook_blocks_tool(hook_client, monkeypatch):
    ws = str(Path(os.environ["GINNO_HOME"]) / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    marker = Path(ws) / "marker.txt"

    model = ScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "touch marker.txt", "workspace": ws})]),
            script(text="blocked, sorry."),
        ]
    )
    monkeypatch.setattr(server, "build_model", lambda *a, **k: model)

    r = hook_client.post("/api/sessions", json={"project_slug": "default", "workspace": ws, "agent_id": "dev"})
    sid = r.json()["id"]

    with WSConversation(hook_client, sid) as conv:
        conv.invoke("run a shell command")
        events = conv.recv_until("message.end", "error")

    names = event_names(events)
    # hook blocks before the interactive permission gate
    assert "permission.request" not in names
    tool_ends = events_of(events, "tool.end")
    assert any("hook blocked" in (e.get("content", "")) for e in tool_ends)
    # the bash command never executed
    assert not marker.exists()
    assert "message.end" in names
