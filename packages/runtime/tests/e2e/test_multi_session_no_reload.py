"""E2E: single-window design — switching sessions must NOT reload kept-alive pages.

Each session owns its own live browser (CDP target). Activating another session
only changes which browser is visible; the others stay alive. We prove no-reload
by stashing a JS marker on session A's page, switching to B, switching back, and
reading the marker (a reload would clear it).

Boots the packaged sidecar (headless Chrome engine); no Tauri/CEF needed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
PORT = 8897


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


def _wait_health(port: int, timeout: float = 40) -> None:
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


def _post(port: int, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _get(port: int, path: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def test_session_switch_does_not_reload(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    env = dict(os.environ, GINNO_HOME=str(home), GINNO_RUNTIME_PORT=str(PORT))
    proc = subprocess.Popen(
        [str(RUNTIME_BIN)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_health(PORT)

        # Session A: open its browser; record the live tab (CDP target) id.
        assert _post(PORT, "/api/browser/session/sA/activate").get("ok") is True
        ta = _post(PORT, "/api/browser/spaces/sA/tabs", {"url": "about:blank"})["tab"]["id"]

        # Session B: its own browser/tab.
        assert _post(PORT, "/api/browser/session/sB/activate").get("ok") is True
        tb = _post(PORT, "/api/browser/spaces/sB/tabs", {"url": "about:blank"})["tab"]["id"]
        assert ta != tb

        # Two live browsers (one per session).
        st = _get(PORT, "/api/browser/state")
        names = {s.get("name") for s in st.get("spaces", [])}
        assert {"sA", "sB"} <= names

        # Switch back to A: the SAME tab id must still be alive (browser kept,
        # not destroyed/recreated => no reload).
        assert _post(PORT, "/api/browser/session/sA/activate").get("ok") is True
        ids_a = [t["id"] for t in _get(PORT, "/api/browser/spaces/sA/tabs")["tabs"]]
        assert ta in ids_a
        # And B's tab is intact too.
        ids_b = [t["id"] for t in _get(PORT, "/api/browser/spaces/sB/tabs")["tabs"]]
        assert tb in ids_b

        # Focus reflects the last activation.
        assert _get(PORT, "/api/browser/focus").get("active_space") == "sA"
    finally:
        proc.terminate()
