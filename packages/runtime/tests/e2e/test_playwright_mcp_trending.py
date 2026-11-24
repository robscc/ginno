"""Run a real Playwright MCP session against a loopback ranking page."""

from __future__ import annotations

import base64
import os
import shutil
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest
from conftest import WSConversation, events_of
from fastapi.testclient import TestClient

from ginno_runtime import jsonx as json
from ginno_runtime import server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

pytestmark = pytest.mark.e2e

NAV = "browser_navigate"
SHOT = "browser_take_screenshot"
NAV_ID = "audit-navigate"
SHOT_ID = "audit-screenshot"
LEADER = "1. Ginno Audit Fixture"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def playwright_mcp(isolated_home: Path, monkeypatch):
    configured = os.environ.get("GINNO_TEST_PLAYWRIGHT_MCP")
    cli = shutil.which(configured) if configured else None
    if not cli:
        reason = "Set GINNO_TEST_PLAYWRIGHT_MCP to the installed @playwright/mcp CLI"
        if configured or os.environ.get("GINNO_PACKAGED_MCP_STRICT") == "1":
            pytest.fail(f"{reason}; configured path: {configured!r}")
        pytest.skip(reason)
    if not shutil.which("node"):
        pytest.fail("Playwright MCP requires node on PATH")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        pytest.fail(f"Python Playwright unavailable: {exc}")
    # Isolate native-host manifests and browser state before TestClient startup.
    os_home = isolated_home / "os-home"
    os_home.mkdir()
    monkeypatch.setenv("HOME", str(os_home))
    with sync_playwright() as pw:
        chromium = pw.chromium.executable_path
    if not Path(chromium).is_file():
        pytest.fail(f"Chromium missing at {chromium}; run `python -m playwright install chromium`")
    # Keep the MCP sockets and Chromium's temporary profile out of a deep pytest TMPDIR.
    with TemporaryDirectory(prefix="ginno-pw-", dir="/tmp" if os.name == "posix" else None) as browser_tmp:
        yield cli, chromium, browser_tmp


@pytest.fixture
def ranking_url(tmp_path, playwright_mcp):
    web = tmp_path / "ranking"
    web.mkdir()
    (web / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<link rel="icon" href="data:,"><title>Audit Trending</title><h1>Audit Trending</h1>'
        f"<p>{LEADER}</p><p>2. Tool Ledger</p></html>",
        encoding="utf-8",
    )
    handler = partial(SimpleHTTPRequestHandler, directory=web)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}/"
        finally:
            http.shutdown()
            thread.join()


def _enable_playwright(home: Path, cli: str, chromium: str, output: Path, browser_tmp: str) -> None:
    (home / "mcp" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "transport": "stdio",
                        "command": cli,
                        "env": {"TMPDIR": browser_tmp, "PWTEST_SOCKETS_DIR": browser_tmp},
                        "args": [
                            "--headless",
                            "--isolated",
                            "--browser",
                            "chromium",
                            "--executable-path",
                            chromium,
                            "--output-dir",
                            str(output),
                        ],
                    },
                }
            }
        )
    )
    (home / "settings.json").write_text(json.dumps({"bypass_permissions": True}))


def _wait_for_playwright(client: TestClient) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get("/api/mcp?tools=1")
        response.raise_for_status()
        status = response.json()
        for row in status["status"]:
            if row["name"] != "playwright":
                continue
            assert not row["error"], f"Playwright MCP connection failed: {row}"
            if row["connected"]:
                assert row["enabled"] and {NAV, SHOT} <= set(status["tools"]), status
                print(f"MCP connected: playwright ({row['tools']} tools, {NAV}, {SHOT})")
                return
        time.sleep(0.2)
    pytest.fail(f"Playwright MCP did not connect: /api/mcp={status!r}")


def _screenshot_from_history(client: TestClient, sid: str):
    # Live tool.end truncates large payloads. History contains the complete image block.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{sid}/history")
        response.raise_for_status()
        for message in response.json()["messages"]:
            for block in message.get("blocks", []):
                if block.get("kind") == "tool" and block.get("id") == SHOT_ID:
                    assert block["name"] == f"mcp_playwright_{SHOT}" and not block.get("failed")
                    for line in block["content"].splitlines():
                        if line.startswith("{"):
                            image = json.loads(line)
                            if image.get("type") == "image":
                                return image
                    pytest.fail(f"Screenshot tool returned no image: {block['content'][:500]}")
        time.sleep(0.1)
    pytest.fail("Screenshot tool result missing from session history")


def test_session_opens_trending_screenshot_and_analyzes(
    isolated_home: Path,
    playwright_mcp,
    ranking_url: str,
    patch_build_model,
):
    cli, chromium, browser_tmp = playwright_mcp
    output = isolated_home / "screenshots"
    output.mkdir()
    _enable_playwright(isolated_home, cli, chromium, output, browser_tmp)
    workspace = isolated_home / "ws"
    workspace.mkdir()

    analysis = f"Audit Trending 排行分析：{LEADER}。"
    patch_build_model(
        ScriptedChatModel(
            scripts=[
                script(
                    text="",
                    tool_calls=[
                        script_tool_call(f"mcp_playwright_{NAV}", {"url": ranking_url}, NAV_ID)
                    ],
                ),
                script(
                    text="",
                    tool_calls=[
                        script_tool_call(
                            f"mcp_playwright_{SHOT}", {"type": "png", "scale": "css"}, SHOT_ID
                        )
                    ],
                ),
                script(text=analysis),
            ]
        )
    )

    with TestClient(server.app) as client:
        _wait_for_playwright(client)
        response = client.post(
            "/api/sessions",
            json={"project_slug": "default", "workspace": str(workspace), "agent_id": "dev"},
        )
        response.raise_for_status()
        sid = response.json()["id"]
        with WSConversation(client, sid) as conv:
            conv.invoke("打开本地项目排行，截图并分析")
            events = conv.recv_until("message.end", "error")

        assert events[-1]["event"] == "message.end", events[-1]
        assert [(e["id"], e["name"]) for e in events_of(events, "tool.start")] == [
            (NAV_ID, f"mcp_playwright_{NAV}"),
            (SHOT_ID, f"mcp_playwright_{SHOT}"),
        ]
        nav, shot = events_of(events, "tool.end")
        assert [nav["id"], shot["id"]] == [NAV_ID, SHOT_ID]
        assert not nav.get("failed") and f"Page URL: {ranking_url}" in nav["content"], nav
        [snapshot] = output.glob("page-*.yml")
        assert snapshot.name in nav["content"] and LEADER in snapshot.read_text(), nav
        assert not shot.get("failed") and "Screenshot" in shot["content"], shot["content"][:500]

        image = _screenshot_from_history(client, sid)
        assert image["mimeType"] == "image/png"
        png = base64.b64decode(image["data"], validate=True)
        assert png.startswith(PNG_SIGNATURE)
        [file] = output.glob("page-*.png")
        assert file.read_bytes() == png
        assert "".join(e["content"] for e in events_of(events, "token.delta")) == analysis
        print(f"{NAV}: {ranking_url} → {LEADER}; {SHOT}: image/png, {len(png)} bytes → {file}")
