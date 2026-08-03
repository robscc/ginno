"""E2E: default Playwright MCP lets a Ginno session operate a real web page.

Flow (as the product intends): in a chat session the agent opens
https://github.com/trending via the Playwright MCP, takes a screenshot, then
produces an analysis of the project ranking. The LLM is scripted (deterministic
tool calls) but the Playwright MCP server + headless Chrome are REAL, so this
genuinely exercises web operation end-to-end.

Skips gracefully when the Playwright MCP / a browser is unavailable in the env.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ginno_runtime import server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

pytestmark = pytest.mark.e2e

NAV = "mcp_playwright_browser_navigate"
SHOT = "mcp_playwright_browser_screenshot"


def _enable_playwright(home: Path) -> None:
    mcp = home / "mcp"
    mcp.mkdir(parents=True, exist_ok=True)
    (mcp / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@playwright/mcp@latest", "--headless", "--browser", "chrome"],
                        "connect_timeout": 60,
                    }
                }
            }
        )
    )
    (home / "settings.json").write_text(json.dumps({"bypass_permissions": True}))


def test_session_opens_trending_screenshot_and_analyzes(isolated_home: Path, monkeypatch):
    home = isolated_home
    _enable_playwright(home)
    ws = home / "ws"
    ws.mkdir(parents=True, exist_ok=True)

    model = ScriptedChatModel(
        scripts=[
            script(text="", tool_calls=[script_tool_call(NAV, {"url": "https://github.com/trending"})]),
            script(text="", tool_calls=[script_tool_call(SHOT, {})]),
            script(text="今日 GitHub Trending 排行：1) 某仓库 …；分析：Python 智能体类项目占主导。"),
        ]
    )

    with TestClient(server.app) as client:
        # /api/mcp lists RAW mcp tool names (browser_navigate); the graph-facing
        # LangChain tools are prefixed mcp_playwright_*. Only skip if truly absent.
        avail = client.get("/api/mcp").json()
        if not any("browser_navigate" in t for t in avail.get("tools", [])):
            pytest.skip("Playwright MCP unavailable (no browser / npx) in this environment")

        # scripted LLM decides the tool calls; Playwright MCP + Chrome are real.
        monkeypatch.setattr(server, "build_model", lambda *a, **k: model)

        r = client.post("/api/sessions", json={"project_slug": "default", "workspace": str(ws), "agent_id": "dev"})
        assert r.status_code == 200
        sid = r.json()["id"]

        events = []
        with client.websocket_connect(f"/api/ws/sessions/{sid}") as w:
            w.send_json({"type": "invoke", "message": "打开 github trending，截图并分析项目排行"})
            while True:
                ev = w.receive_json()
                events.append(ev)
                if ev.get("event") in ("message.end", "error"):
                    break

    names = [e.get("event") for e in events]
    # tool.start carries the tool name; tool.end only carries id+content.
    tool_names = " ".join(str(e.get("name")) for e in events if e.get("event") == "tool.start")
    assert "mcp_playwright_browser_navigate" in tool_names, (names, tool_names)
    assert "mcp_playwright_browser_screenshot" in tool_names, (names, tool_names)
    # both tools actually returned results
    assert sum(1 for e in events if e.get("event") == "tool.end") >= 2
    # final analysis text streamed
    text = "".join(str(e.get("content", "")) for e in events if e.get("event") == "token.delta")
    assert "Trending" in text or "排行" in text
