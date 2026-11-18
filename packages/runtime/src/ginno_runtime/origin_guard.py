"""Loopback origin guard — the P0 fix of security-audit (2026-11-12).

Threat model (audit, V3 P0-1): ANY web page open in the user's browser can
drive the whole 8787 API — CSRF-style POSTs (proxy RCE via session create →
invoke → self-approve on the same WS), reads (folders register + code/read
bypass key masking) and relay hijack. Browsers ALWAYS attach ``Origin`` to
cross-origin fetch/WebSocket handshakes (it is a forbidden header — JS cannot
forge it), so an allowlist on ``Origin`` severs every browser-driven vector
while local non-browser clients (curl, harness probes, rtk, e2e drivers)
send no ``Origin`` at all and stay allowed.

Scope of this layer: browser pages. A hostile *local process* reading files
or speaking plain HTTP is a different layer (loopback token, follow-up) —
including a hostile local HTTP server luring the browser to a
``http://127.0.0.1:<port>`` page, which is why loopback-any-port is an
acceptable allow (the browser cannot be made to label a remote page with a
loopback origin; DNS-rebinding pages keep their hostname as Origin).

Allowlist:
- loopback hosts on ANY http(s) port (``127.0.0.1`` / ``localhost`` /
  ``[::1]``) — packaged e2e drives sidecars on ``bind(0)`` random ports and
  same-origin POST fetches carry exactly that Origin;
- the app's webview origins (``tauri://localhost`` macOS/Linux,
  ``https://tauri.localhost`` Windows WebView2);
- dev web (``http://localhost:3000`` — covered by loopback-any-port, kept
  documented here);
- ``chrome-extension://*`` — the browser-companion relay (``/extension``)
  is driven by the user's own extension whose id is not knowable up front;
- no ``Origin`` at all (non-browser clients);
- extra exact origins via ``GINNO_ALLOWED_ORIGINS`` (comma-separated).
"""

from __future__ import annotations

import os
import re

from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "tauri://localhost",
    "https://tauri.localhost",
)

# http(s)://127.0.0.1 | localhost | [::1] :<any port>
LOOPBACK_ORIGIN_RE = re.compile(
    r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
)


def allowed_origins() -> frozenset[str]:
    env = os.environ.get("GINNO_ALLOWED_ORIGINS", "")
    extra = {o.strip() for o in env.split(",") if o.strip()}
    return frozenset(DEFAULT_ALLOWED_ORIGINS) | extra


def origin_allowed(origin: str | None) -> bool:
    """None (non-browser client) → allowed; otherwise must match the list or
    the loopback-any-port / ``chrome-extension://`` rules."""
    if not origin:
        return True
    if origin in allowed_origins():
        return True
    if LOOPBACK_ORIGIN_RE.match(origin):
        return True
    return origin.startswith("chrome-extension://")


def _scope_origin(scope: Scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"origin":
            return v.decode("latin-1")
    return None


class OriginGuardMiddleware:
    """Pure-ASGI guard: HTTP 403 JSON / WS handshake 1008 for off-list origins.

    Mounted outermost on the main app (after every ``add_middleware`` call so
    it runs before CORS/SecurityHeaders). Registered routes stay untouched;
    the five WS endpoints get covered here in one place instead of per-accept
    checks (the session WS alone has three accept branches).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            if not origin_allowed(_scope_origin(scope)):
                from fastapi.responses import JSONResponse

                await JSONResponse(
                    status_code=403,
                    content={"ok": False, "error": "origin not allowed"},
                )(scope, receive, send)
                return
        elif scope.get("type") == "websocket":
            if not origin_allowed(_scope_origin(scope)):
                # Rejecting the handshake before accept → uvicorn answers the
                # upgrade request with HTTP 403 (no WS is ever established).
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,  # policy violation
                        "reason": "origin not allowed",
                    }
                )
                return
        await self.app(scope, receive, send)
