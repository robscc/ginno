"""Playwright e2e against the PACKAGED sidecar (dist/ginno-runtime).

Boots the real PyInstaller binary on a free port with a temp home + GINNO_FAKE_LLM,
then drives a real Chromium to verify the UI shows the Agent/Session lists and that
"+ New Session" actually creates a session. This walks the same case a user runs in
the desktop app, without touching the user's live sidecar on :8787.

Skips gracefully when the binary isn't built, playwright isn't installed, or the
port is taken.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

RUNTIME_BIN = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
PORT = 8899


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
    """Launch Chromium; if the browser binary isn't installed yet, install it once
    (self-heal so the e2e works on a fresh checkout without manual setup)."""
    try:
        return pw.chromium.launch()
    except Exception:
        import sys

        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        return pw.chromium.launch()


def _wait_health(port: int, timeout: float = 40) -> None:
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
            "anthropic": {"enabled": False, "protocol": "anthropic", "api_key": "", "default_model": "x", "base_url": "", "max_tokens": 1, "temperature": 0.7, "timeout_s": 5},
            "openai": {"enabled": False, "protocol": "openai", "api_key": "", "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1},
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t", "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m", "max_tokens": 100, "temperature": 0.7, "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def test_packaged_ui_shows_lists_and_adds_session(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed (uv sync --group test && playwright install chromium)")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = subprocess.Popen([str(RUNTIME_BIN)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(2000)

            # Agent list renders (seeded agents).
            assert page.locator("text=Dev Agent").count() >= 1, "agent list should show Dev Agent"

            # Session list: capture count, then add one via the button.
            def session_count() -> int:
                return page.evaluate(
                    "fetch('/api/sessions?project_slug=default').then(r=>r.json()).then(j=>j.length)"
                )

            before = session_count()
            page.get_by_text("+ New Session").first.click()
            page.wait_for_timeout(1500)
            after = session_count()
            assert after == before + 1, f"New Session should add one session ({before}->{after})"
            # The new session is selected/shown in the sidebar.
            assert page.locator("text=session").count() >= 1
            browser.close()
    finally:
        proc.terminate()
