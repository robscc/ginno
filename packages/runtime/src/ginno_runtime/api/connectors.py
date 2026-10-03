"""Connector endpoints (connector-module-design.md §5).

- GET  /api/connectors            — list + aggregate dot
- GET  /api/connectors/{id}       — one connector (status + config + install steps)
- PATCH /api/connectors/{id}/config — set_config (schema-checked, lenient)
- POST /api/connectors/{id}/action — module actions: reveal_folder /
  browser_handoff_release / confirm_domain / profile_start / profile_stop
"""

from __future__ import annotations

import subprocess
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import paths
from ..connectors import ensure_builtin_connectors
from ..connectors import registry as conn_registry

router = APIRouter()


@router.on_event("startup")
async def _seed() -> None:
    ensure_builtin_connectors()


# ---- 扩展轨 relay(设计 §3):扩展经 /extension(/v2 别名)连入 -------------

@router.websocket("/extension")
@router.websocket("/extension/v2")
async def extension_relay(ws: WebSocket) -> None:
    from ..browser.relay import extension_endpoint

    await extension_endpoint(ws)


# ---- 连接器事件通道(connector §5 的推送半边;轮询自此降级为兜底) ------

@router.websocket("/api/ws/connectors")
async def connectors_events_ws(ws: WebSocket) -> None:
    import asyncio

    from ..connectors.events import connector_events, wire_default_producers

    ensure_builtin_connectors()
    wire_default_producers()
    await ws.accept()
    ev_bus = connector_events()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _listener(type_: str, data: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"type": type_, **data})

    ev_bus.subscribe(_listener)
    try:
        # snapshot first: list + latest pushed page + latest progress
        reg = conn_registry()
        await ws.send_json({
            "type": "snapshot",
            "connectors": reg.list_payload(),
            "latestPage": ev_bus.latest_page,
            "latestProgress": ev_bus.latest_progress,
        })
        while True:
            msg = await queue.get()
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — client vanished mid-send
        pass
    finally:
        ev_bus.unsubscribe(_listener)


@router.get("/api/connectors/browser/pushed-page")
async def pushed_page() -> dict:
    from ..connectors.events import connector_events, wire_default_producers

    wire_default_producers()
    return {"page": connector_events().latest_page}


@router.get("/api/connectors")
async def list_connectors() -> dict:
    ensure_builtin_connectors()
    reg = conn_registry()
    return reg.list_payload()


@router.get("/api/connectors/{cid}")
async def connector_detail(cid: str) -> Any:
    ensure_builtin_connectors()
    reg = conn_registry()
    conn = reg.get(cid)
    if conn is None:
        return JSONResponse({"error": f"unknown connector: {cid}"}, status_code=404)
    payload = reg.status_of(cid)
    payload["config"] = reg.read_config(cid)
    payload["configSchema"] = conn.config_schema()
    payload["installSteps"] = conn.install_steps()
    return payload


@router.patch("/api/connectors/{cid}/config")
async def update_connector_config(cid: str, body: dict) -> Any:
    ensure_builtin_connectors()
    reg = conn_registry()
    conn = reg.get(cid)
    if conn is None:
        return JSONResponse({"error": f"unknown connector: {cid}"}, status_code=404)
    if not isinstance(body, dict) or not body:
        return JSONResponse({"error": "body must be a non-empty object"}, status_code=400)
    schema = conn.config_schema()
    props = (schema or {}).get("properties") or {}
    clean = {k: v for k, v in body.items() if k in props}
    unknown = [k for k in body if k not in props and k != "confirmed_domains"]
    if unknown:
        return JSONResponse(
            {"error": f"unknown config keys: {', '.join(unknown)}"}, status_code=400)
    reg.write_config(cid, clean)
    try:
        await conn.apply_config(reg.read_config(cid))
    except Exception:  # noqa: BLE001 — apply hooks are best-effort
        pass
    return {"ok": True, "config": reg.read_config(cid)}


@router.post("/api/connectors/{cid}/action")
async def connector_action(cid: str, body: dict) -> Any:
    ensure_builtin_connectors()
    reg = conn_registry()
    action = (body or {}).get("action", "")
    if action == "reveal_folder":
        target = paths.home() / (body.get("folder") or "browser-extension")
        try:
            target.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["open", "-R", str(target)])
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
    if action == "browser_handoff_release":
        from ..tools.browser_tools import release_handoff

        ok = release_handoff(body.get("tabId", cid))
        return {"ok": bool(ok)}
    if action == "confirm_domain":
        # 受保护域名确认(设计 §6):加入 chrome-extension 的 confirmed_domains
        domain = (body.get("domain") or "").strip().lower()
        if not domain:
            return JSONResponse({"error": "domain required"}, status_code=400)
        reg.write_config("chrome-extension", {})  # ensure section exists
        settings_path = paths.settings_path()
        import json as _json

        settings = _json.loads(settings_path.read_text() or "{}")
        conns = settings.setdefault("connectors", {})
        ce = conns.setdefault("chrome-extension", {})
        confirmed = ce.setdefault("confirmed_domains", [])
        if domain not in confirmed:
            confirmed.append(domain)
        settings_path.write_text(_json.dumps(settings, ensure_ascii=False, indent=2))
        return {"ok": True, "confirmedDomains": confirmed}
    if action == "profile_start":
        from ..browser.executor import get_profile_backend

        b = get_profile_backend()
        try:
            await b.ensure_running()
            reg.report("browser-profile", "connected", "运行中")
            return {"ok": True, "tabs": await b.tabs_context()}
        except Exception as e:  # noqa: BLE001
            reg.report("browser-profile", "error", str(e))
            return {"ok": False, "error": str(e)}
    if action == "profile_stop":
        from ..browser.executor import get_profile_backend

        b = get_profile_backend()
        await b.shutdown()
        reg.report("browser-profile", "disconnected", "已停止")
        return {"ok": True}
    if action == "extension_status_refresh":
        from ..browser.relay import relay_state

        return {"ok": True, "relay": relay_state().snapshot()}
    return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)


# ---- browser handoff (设计 M3): tool 阻塞等待接管,UI 查询 + 释放 ----------

@router.get("/api/connectors/browser/handoff")
async def handoff_status() -> dict:
    from ..tools.browser_tools import _handoff_events

    return {"active": [
        {"tabId": k, "since": None} for k in _handoff_events.keys()
    ]}


# ---- MCP-shaped exposure (设计 M3「占协议端点」): POST /mcp ----------------
# Streamable-HTTP-flavoured JSON-RPC: initialize / tools/list / tools/call.
# Third-party harnesses (dsh-style) can drive Ginno's browser channel through
# the same dispatch the internal tools use — extension track preferred.

_MCP_PROTOCOL = "2024-11-05"


def _mcp_tools_payload() -> list[dict]:
    from ..tools.browser_tools import BROWSER_TOOL_NAMES, build_browser_tools

    out = []
    for t in build_browser_tools():
        if t.name not in BROWSER_TOOL_NAMES:
            continue
        try:
            schema = t.args_schema.schema() if hasattr(t.args_schema, "schema") else (t.args or {})
        except Exception:  # noqa: BLE001
            schema = t.args or {}
        out.append({"name": t.name, "description": t.description or "",
                    "inputSchema": schema})
    return out


async def _mcp_call_tool(name: str, args: dict) -> dict:
    from ..browser.cdp import CDPError
    from ..browser.relay import RelayError
    from ..tools.browser_tools import _dispatch

    try:
        if name == "browser_handoff":
            return {"content": [{"type": "text", "text": (
                "handoff 等待不适用于外部调用;请用 browser_stop 等工具。")}]}
        res = await _dispatch(name, args)
        if isinstance(res, list):  # already content blocks
            texts = [b.get("text", "") for b in res
                     if isinstance(b, dict) and b.get("type") == "text"]
            images = [b for b in res
                      if isinstance(b, dict) and b.get("type") == "image_url"]
            content = [{"type": "text", "text": "\n".join(t for t in texts if t)}]
            for b in images:
                url = (b.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    mime, _, data = url[5:].partition(";base64,")
                    content.append({"type": "image", "data": data,
                                    "mimeType": mime or "image/jpeg"})
            return {"content": content}
        return {"content": [{"type": "text", "text": str(res)}]}
    except (CDPError, RelayError) as e:
        return {"content": [{"type": "text", "text": f"[error] {e}"}], "isError": True}


@router.post("/mcp")
async def mcp_endpoint(body: dict) -> Any:
    method = body.get("method")
    mid = body.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": _MCP_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ginno-browser", "version": "1.0.0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"tools": _mcp_tools_payload()}}
    if method == "tools/call":
        p = body.get("params") or {}
        result = await _mcp_call_tool(str(p.get("name", "")), p.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}
