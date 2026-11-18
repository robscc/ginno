"""P0-1 (security-audit 2026-11-12) regression: loopback origin guard.

Drive the real middleware on a mini app (one GET + one WS) plus static
assertions against the main server's CORS config. Threat model: a page open
in the user's browser driving 8787 — browsers always attach ``Origin``
(forbidden header, unforgeable), so an allowlist severs every browser vector
while local non-browser clients (no Origin header) stay allowed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient

from ginno_runtime.origin_guard import (
    DEFAULT_ALLOWED_ORIGINS,
    OriginGuardMiddleware,
    origin_allowed,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def mini():
    app = FastAPI()
    app.add_middleware(OriginGuardMiddleware)

    @app.get("/api/ping")
    async def _ping():
        return {"ok": True}

    @app.websocket("/api/ws/echo")
    async def _echo(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    return TestClient(app)


# ---- allowlist decision table -----------------------------------------------
@pytest.mark.parametrize(
    "origin,ok",
    [
        (None, True),  # curl / harness / rtk — non-browser clients
        ("", True),
        ("tauri://localhost", True),  # packaged webview (tauri protocol)
        ("https://tauri.localhost", True),  # Windows WebView2
        ("http://127.0.0.1:8787", True),  # packaged webview (same-origin)
        ("http://localhost:3000", True),  # pnpm dev
        ("chrome-extension://abcdefABCDEF1234567890abcdef", True),  # relay
        ("https://evil.example", False),  # drive-by page
        ("http://127.0.0.1:54321", True),  # e2e bind(0) random port, same-origin
        ("http://localhost:3000", True),  # covered by loopback-any-port
        ("http://127.0.0.1.evil.example", False),  # suffix trick
        ("https://evil.example:127.0.0.1", False),
        ("http://127.0.0.1:3000.evil.example", False),  # suffix trick
        ("null", False),  # sandboxed iframe — treat as hostile
        ("", True),
    ],
)
def test_origin_allowed(origin, ok):
    assert origin_allowed(origin) is ok


def test_env_extra_origins(monkeypatch):
    monkeypatch.setenv("GINNO_ALLOWED_ORIGINS", "https://ci.example, ")
    assert origin_allowed("https://ci.example") is True
    assert origin_allowed("https://other.example") is False


# ---- http layer --------------------------------------------------------------
def test_http_evil_origin_403_json(mini):
    r = mini.get("/api/ping", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json() == {"ok": False, "error": "origin not allowed"}


def test_http_no_origin_passes(mini):
    assert mini.get("/api/ping").status_code == 200


def test_http_tauri_origin_passes(mini):
    r = mini.get("/api/ping", headers={"Origin": "tauri://localhost"})
    assert r.status_code == 200


# ---- ws handshake layer ------------------------------------------------------
def test_ws_evil_origin_rejected(mini):
    """The relay-hijack / proxy-RCE vector: a browser page opening the WS
    directly. The handshake must be refused (HTTP 403, no WS established)."""
    with pytest.raises(Exception):
        with mini.websocket_connect(
            "/api/ws/echo", headers={"Origin": "https://evil.example"}
        ):
            pass


def test_ws_tauri_origin_accepted(mini):
    with mini.websocket_connect(
        "/api/ws/echo", headers={"Origin": "tauri://localhost"}
    ) as ws:
        assert ws.receive_text() == "hi"


def test_ws_no_origin_accepted(mini):
    with mini.websocket_connect("/api/ws/echo") as ws:
        assert ws.receive_text() == "hi"


# ---- main server wiring -------------------------------------------------------
def test_main_app_cors_not_wildcard():
    import ginno_runtime.server as server_mod

    cors = [
        m
        for m in server_mod.app.user_middleware
        if m.cls.__name__ == "CORSMiddleware"
    ]
    assert cors, "CORSMiddleware must stay mounted"
    opts = cors[0].kwargs  # starlette Middleware stores init kwargs here
    assert "*" not in opts.get("allow_origins"), "wildcard origin is the P0 hole"
    assert "tauri://localhost" in opts["allow_origins"]
    assert opts.get("allow_origin_regex"), "loopback regex must cover dev/e2e ports"
    guards = [
        m
        for m in server_mod.app.user_middleware
        if m.cls is OriginGuardMiddleware
    ]
    assert guards, "OriginGuardMiddleware must be mounted on the main app"


def test_default_origins_have_no_wildcard_or_schemeless():
    for o in DEFAULT_ALLOWED_ORIGINS:
        assert "://" in o, f"origin must be scheme-qualified: {o}"
        assert "*" not in o
