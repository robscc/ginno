"""Playwright e2e: the pin (/pin) quick-chat window against the PACKAGED sidecar.

Refactor batch 3-1 gave PinStream the MAIN window's streamCore.applyBlock via
pinApplyBlock (no more drifting local copy). This is the smallest end-to-end
walk of that path in a REAL browser: the pin auto-creates its quick session,
one scripted turn streams TWO same-name bash tool calls (the exact shape that
broke the old tool.end targeting) plus a closing text delta, and the transcript
must show BOTH tool results — none dropped, none mis-targeted, none stuck
pending — and the closing text. A reload then replays the same turn from
history through the same render layer (mapHistory path).

Skips gracefully when the binary isn't built, playwright isn't installed, or
the port is taken (same contract as the sibling packaged-UI files).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
# Siblings own 8899/8898/8897/8896 — this one is ours.
PORT = 8895

CLOSING_TEXT = "两次都跑完了。"


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        import sys

        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        return pw.chromium.launch()


def _wait_health(port: int, timeout: float = 60) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"sidecar on :{port} did not become healthy")


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "providers": {
            "anthropic": {
                "enabled": False, "protocol": "anthropic", "api_key": "",
                "default_model": "x", "base_url": "", "max_tokens": 1,
                "temperature": 0.7, "timeout_s": 5,
            },
            "openai": {
                "enabled": False, "protocol": "openai", "api_key": "",
                "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1,
            },
            "custom": {
                "enabled": True, "protocol": "openai-compatible", "name": "t",
                "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                "max_tokens": 100, "temperature": 0.7, "timeout_s": 5,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _scripts(path: Path) -> None:
    """One scripted turn: TWO same-name bash calls in LLM batches 1 and 2 (the
    shape that broke the pre-2026-11-15 tool.end targeting — same-name retries
    within one turn), then a closing text delta.

    NOTE the two calls are in SEPARATE script entries, not one batch:
    fake_model's streaming splitter hardcodes tool_call_chunks index 0 for
    every call, so a second same-batch call's args chunk would be CONCATENATED
    into the first call's args (corrupt JSON → "Field required" server-side).
    One tool_call per batch is the supported shape (same as every other
    fake-LLM e2e); the tool.end targeting scenario only needs both calls to
    land in the SAME TURN, which separate batches still are."""
    path.write_text(
        json.dumps(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "bash", "args": {"command": "echo first-out"}},
                    ],
                },
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "bash", "args": {"command": "echo second-out"}},
                    ],
                },
                {"content": CLOSING_TEXT},
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip(
            "playwright not installed (uv sync --group test && playwright install chromium)"
        )
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    _scripts(scripts)

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/pin", wait_until="load")
            page.wait_for_timeout(1500)
            yield page
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _wait_text(page, text: str, timeout_ms: int = 30000):
    """Poll for visible text (CI-slow friendly — never assert once and pray)."""
    page.wait_for_selector(f"text={text}", timeout=timeout_ms, state="visible")


def test_pin_streams_two_same_name_tools_to_completion(app):
    page = app
    # PinApp auto-creates the quick session → the composer appears.
    box = page.locator("textarea")
    box.wait_for(state="visible", timeout=15000)

    box.fill("跑两遍")
    box.press("Enter")

    # Both same-name bash results must render (old tool.end targeting dropped
    # or mis-targeted one of them), plus the closing text delta.
    _wait_text(page, "first-out")
    _wait_text(page, "second-out")
    _wait_text(page, CLOSING_TEXT)

    # Turn closed → no tool row stuck pending (the amber spinner glyph).
    page.wait_for_timeout(800)
    spins = page.locator("svg.animate-spin").count()
    assert spins == 0, f"tool rows still pending after turn end ({spins} spinners)"

    # History replay path (reload → mapHistory → same render layer): the turn
    # survives with both tool results and the closing text.
    page.reload(wait_until="load")
    page.wait_for_timeout(1500)
    _wait_text(page, "first-out")
    _wait_text(page, "second-out")
    _wait_text(page, CLOSING_TEXT)
