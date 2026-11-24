"""Verify packaged Context Manager against a local stdio MCP server."""

import os
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime"
SERVER = "audit-context"
TOOL = "rank_project"

# Discover a real tool schema over initialize/tools/list without depending on npm.
MCP_SERVER = '''
from mcp.server.fastmcp import FastMCP

server = FastMCP("audit-context")

@server.tool()
def rank_project(project: str) -> str:
    """Look up the rank of a project in the audit fixture."""
    return f"{project}: 1"

server.run(transport="stdio")
'''


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "en",
        "providers": {
            "anthropic": {
                "enabled": False,
                "protocol": "anthropic",
                "api_key": "",
                "default_model": "x",
                "base_url": "",
                "max_tokens": 1,
                "temperature": 0.7,
                "timeout_s": 5,
            },
            "openai": {
                "enabled": False,
                "protocol": "openai",
                "api_key": "",
                "default_model": "x",
                "base_url": "",
                "org_id": "",
                "max_tokens": 1,
            },
            "custom": {
                "enabled": True,
                "protocol": "openai-compatible",
                "name": "t",
                "api_key": "k",
                "base_url": "http://127.0.0.1:1",
                "model": "m",
                "max_tokens": 100,
                "temperature": 0.7,
                "timeout_s": 5,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _wait_for_mcp(origin, proc, log_path):
    deadline = time.monotonic() + 40
    status = None
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{origin}/api/mcp?tools=1", timeout=2) as response:
                status = json.loads(response.read())
        except OSError as exc:
            status = str(exc)
        else:
            for row in status["status"]:
                if row["name"] == SERVER and row["connected"] and TOOL in status["tools"]:
                    assert row["enabled"] and row["tools"] == 1, row
                    return row
        time.sleep(0.2)
    pytest.fail(
        f"Packaged sidecar {origin} did not discover {SERVER}/{TOOL}. "
        f"Exit={proc.poll()}, last /api/mcp={status!r}\n{log_path.read_text()[-4000:]}"
    )


def test_packaged_ui_context_panel_ledger_and_search(tmp_path):
    runtime_bin = Path(os.environ.get("GINNO_TEST_PACKAGED_RUNTIME", str(RUNTIME_BIN)))
    missing = None
    if not runtime_bin.is_file():
        missing = f"packaged sidecar missing at {runtime_bin} (run `make runtime`)"
    else:
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError as exc:
            missing = f"Python Playwright unavailable: {exc}"
    if missing:
        if os.environ.get("GINNO_PACKAGED_MCP_STRICT") == "1":
            pytest.fail(missing)
        pytest.skip(missing)

    home = tmp_path / "home"
    ginno_home = tmp_path / "ginno"
    home.mkdir()
    (ginno_home / "mcp").mkdir(parents=True)
    mcp_script = tmp_path / "mcp_server.py"
    mcp_script.write_text(MCP_SERVER, encoding="utf-8")
    (ginno_home / "mcp" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    SERVER: {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(mcp_script)],
                    },
                }
            }
        )
    )
    (ginno_home / "settings.json").write_text(json.dumps(_settings()))

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = dict(
        os.environ,
        HOME=str(home),
        GINNO_HOME=str(ginno_home),
        GINNO_FAKE_LLM="1",
        GINNO_RUNTIME_PORT=str(port),
    )
    env.pop("TIKTOKEN_CACHE_DIR", None)
    log_path = tmp_path / "sidecar-stderr.log"
    with log_path.open("wb") as log:
        proc = sc.spawn_sidecar(str(runtime_bin), env, err=log)
        try:
            connected = _wait_for_mcp(origin, proc, log_path)
            [tool] = connected["toolDetails"]
            assert tool["name"] == TOOL, tool
            print(f"MCP connected: {SERVER} ({origin}), tool={tool['fullName']}")
            with sync_playwright() as pw:
                if not Path(pw.chromium.executable_path).is_file():
                    missing = "Chromium missing; run `python -m playwright install chromium`"
                    if os.environ.get("GINNO_PACKAGED_MCP_STRICT") == "1":
                        pytest.fail(missing)
                    pytest.skip(missing)
                browser = pw.chromium.launch(
                    channel="chromium", env=dict(os.environ, HOME=str(home))
                )
                try:
                    page = browser.new_page(locale="en-US")
                    page.goto(origin, wait_until="load")
                    composer = page.locator("textarea").first
                    with page.expect_response(
                        lambda r: r.url == f"{origin}/api/sessions" and r.request.method == "POST"
                    ) as created:
                        composer.fill("hello context panel")
                        composer.press("Enter")
                    assert created.value.ok
                    sid = created.value.json()["id"]
                    session_url = f"{origin}/api/sessions/{sid}"

                    trigger = page.get_by_title("Context Manager", exact=True)
                    panel = page.locator("div.absolute.bottom-full").filter(has_text="per request")
                    with page.expect_response(f"{session_url}/context_breakdown") as ledger:
                        trigger.click()
                    assert ledger.value.ok
                    breakdown = ledger.value.json()
                    assert breakdown["ok"] and breakdown["counter"] == "tiktoken", breakdown
                    [part] = breakdown["tools"]
                    [measured] = part["byTool"]
                    tokens = measured["tokens"]
                    assert part["server"] == SERVER and part["connected"], part
                    assert measured == {"name": tool["fullName"], "enabled": True, "tokens": tokens}
                    assert breakdown["toolsTotal"] == tokens > 0, breakdown
                    subtotal = panel.get_by_text(re.compile(r"^tools ≈"))
                    expect(subtotal).to_have_text(f"tools ≈{tokens}")
                    expect(panel.get_by_text("estimated", exact=False)).to_have_count(0)
                    expect(panel.get_by_text("MCP Servers · 1", exact=True)).to_be_visible()

                    search = panel.get_by_placeholder("search servers / tools / skills")
                    switch = panel.get_by_role("switch", name=SERVER, exact=True)
                    expect(switch).to_be_checked()
                    search.fill("no-audit-match")
                    expect(switch).to_have_count(0)
                    search.fill(TOOL)
                    expect(panel.get_by_role("checkbox", name=TOOL, exact=True)).to_be_visible()
                    expect(switch).to_be_checked()
                    search.fill("")

                    policy_url = f"{session_url}/tool_policy"
                    # Click the row outside the switch and verify the saved policy on reopen.
                    with page.expect_response(policy_url) as disabled:
                        panel.get_by_text(f"1 · ≈{tokens}", exact=True).click()
                    assert disabled.value.ok
                    disabled_policy = disabled.value.json()
                    assert disabled_policy["ok"] and disabled_policy["mcp_disabled"] == [SERVER]
                    expect(switch).not_to_be_checked()
                    expect(subtotal).to_have_text("tools ≈0")
                    done = panel.get_by_role("button", name="Done", exact=True)
                    done.click()
                    expect(panel).to_have_count(0)
                    with page.expect_response(f"{session_url}/context_breakdown") as reopened:
                        trigger.click()
                    assert reopened.value.json()["toolsTotal"] == 0
                    expect(switch).not_to_be_checked()

                    with page.expect_response(policy_url) as restored:
                        panel.get_by_text(SERVER, exact=True).click()
                    assert restored.value.ok
                    restored_policy = restored.value.json()
                    assert restored_policy["ok"] and restored_policy["mcp_disabled"] == []
                    response = page.request.get(f"{session_url}/context_breakdown")
                    assert response.ok and response.json()["toolsTotal"] == tokens
                    expect(switch).to_be_checked()
                    expect(subtotal).to_have_text(f"tools ≈{tokens}")
                    page.screenshot(path=tmp_path / "context-manager.png")
                    done.click()
                    expect(panel).to_have_count(0)
                    print(f"Packaged Context Manager token estimate: {tokens} → 0 → {tokens}")
                finally:
                    browser.close()
        finally:
            sc.stop_sidecar(proc)
