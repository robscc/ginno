"""The /extension WS relay — sidecar side (browser-companion-extension-design.md §3).

The companion extension connects here and becomes the preferred browser
backend (扩展轨). Protocol frames are MCP-flavoured JSON-RPC:

    ext → ginno:  {method:"extensionInfo", params:{version,browserType,
                   capabilities,browserClientId}}
    ginno → ext:  {method:"ping"} / {id, method:"tools/invoke",
                   params:{tool, arguments}}
    ext → ginno:  {method:"pong"} / {id, result|error} /
                   {method:"tools/progress", params:{...}}
    ext → ginno:  {method:"stopToolExecution"}  (页内 Stop 按钮)

Tool invocations from the tools layer are serialized through a FIFO queue
with per-command wait timeout (QUEUE_FULL / QUEUE_TIMEOUT, 设计 §3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect

from ..connectors import registry as conn_registry
from ..connectors.registry import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_ERROR,
)

log = logging.getLogger("ginno.browser.relay")

QUEUE_CAP = 32
INVOKE_TIMEOUT_S = 60.0
SCREENSHOT_TIMEOUT_S = 90.0
PROGRESS_CAP_S = 90.0


class RelayState:
    """Everything the sidecar knows about the connected extension."""

    def __init__(self) -> None:
        self.ws: WebSocket | None = None
        self.version: str | None = None
        self.browser_type: str | None = None
        self.browser_client_id: str | None = None
        self.capabilities: list[str] = []
        self.transport: str = ""        # "websocket" | "native-messaging"
        self.connected_at: float | None = None
        self._queue: asyncio.Queue[tuple[int, dict, asyncio.Future]] = asyncio.Queue(QUEUE_CAP)
        self._worker: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._progress_cb: Callable[[dict], None] | None = None
        self._stop_flags: set[int] = set()

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "version": self.version,
            "browserType": self.browser_type,
            "browserClientId": self.browser_client_id,
            "capabilities": self.capabilities,
            "transport": self.transport,
            "connectedAt": self.connected_at,
        }


_state = RelayState()


def relay_state() -> RelayState:
    return _state


def set_progress_listener(fn: Callable[[dict], None]) -> None:
    _state._progress_cb = fn


async def invoke_tool(tool: str, arguments: dict,
                      slow: bool = False) -> dict:
    """Route one tool call to the extension. Raises RelayError on failure."""
    if not _state.connected:
        raise RelayError("浏览器扩展未连接。请打开 连接器 页面查看安装指引,"
                         "或让用户用 Ginno 自带的浏览器实例。")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    try:
        _state._queue.put_nowait((_state._next_id, {"tool": tool, "arguments": arguments}, fut))
    except asyncio.QueueFull:
        raise RelayError("工具命令队列已满(QUEUE_FULL),请稍后重试。") from None
    mid = _state._next_id
    _state._next_id += 1
    timeout = SCREENSHOT_TIMEOUT_S if slow else INVOKE_TIMEOUT_S
    try:
        return await asyncio.wait_for(fut, timeout)
    except TimeoutError:
        _state._pending.pop(mid, None)
        raise RelayError("扩展执行超时(QUEUE_TIMEOUT)。若页面卡住,可让用户检查 Chrome。"
                         ) from None


class RelayError(RuntimeError):
    pass


def request_stop() -> None:
    """页内 Stop 按钮 / 前端停止:中断当前正在执行的扩展调用。"""
    _state._stop_flags.update(_state._pending.keys())
    if _state.connected and _state.ws is not None:
        try:
            asyncio.ensure_future(_state.ws.send_text(json.dumps(
                {"method": "stopToolExecution"})))
        except Exception:  # noqa: BLE001
            pass


def _report_status(detail: str = "", status: str | None = None) -> None:
    reg = conn_registry()
    st = status or (STATUS_CONNECTED if _state.connected else STATUS_DISCONNECTED)
    reg.report(
        "chrome-extension",
        st,
        detail or ("已连接" if _state.connected else "扩展未连接(未安装或已禁用)"),
        version=_state.version,
        capabilities=_state.capabilities,
        extra={
            "browserType": _state.browser_type,
            "browserClientId": _state.browser_client_id,
            "transport": _state.transport,
        },
    )


async def _queue_worker() -> None:
    while True:
        mid, params, fut = await _state._queue.get()
        if _state.ws is None or fut.done():
            if not fut.done():
                fut.set_exception(RelayError("扩展连接已断开"))
            continue
        try:
            await _state.ws.send_text(json.dumps(
                {"id": mid, "method": "tools/invoke", "params": params}))
            _state._pending[mid] = fut
        except Exception as e:  # noqa: BLE001
            if not fut.done():
                fut.set_exception(RelayError(f"发送到扩展失败: {e}"))


async def extension_endpoint(ws: WebSocket) -> None:
    """FastAPI websocket handler mounted at /extension (and /extension/v2)."""
    await ws.accept()
    prev = _state.ws
    if prev is not None:
        try:
            await prev.close()
        except Exception:  # noqa: BLE001
            pass
    _state.ws = ws
    _state.transport = "websocket"
    _state.connected_at = time.time()
    _state.capabilities = ["mcp-tools", "fifo-command-queue", "tool-progress"]
    if _state._worker is None or _state._worker.done():
        _state._worker = asyncio.create_task(_queue_worker())

    async def _pinger() -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send_text(json.dumps({"method": "ping"}))
            except Exception:  # noqa: BLE001
                return

    pinger = asyncio.create_task(_pinger())
    _report_status()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            method = msg.get("method")
            if method == "extensionInfo":
                p = msg.get("params") or {}
                _state.version = p.get("version") or p.get("extensionVersion")
                _state.browser_type = p.get("browserType")
                _state.browser_client_id = p.get("browserClientId")
                if isinstance(p.get("capabilities"), list):
                    _state.capabilities = p["capabilities"]
                _report_status("已连接 · Chrome"
                               if _state.connected else "扩展未连接")
            elif method == "pong":
                continue
            elif method == "stopToolExecution":
                request_stop()
            elif method == "tools/progress":
                if _state._progress_cb:
                    try:
                        _state._progress_cb(msg.get("params") or {})
                    except Exception:  # noqa: BLE001
                        pass
            elif isinstance(msg.get("id"), int | float):
                mid = int(msg["id"])
                fut = _state._pending.pop(mid, None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(RelayError(
                        f"{err.get('code', 'EXTENSION_ERROR')}: {err.get('message', '')}"))
                else:
                    fut.set_result(msg.get("result") or {})
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("extension relay ended: %s", e)
    finally:
        pinger.cancel()
        if _state.ws is ws:
            _state.ws = None
            for fut in _state._pending.values():
                if not fut.done():
                    fut.set_exception(RelayError("扩展连接已断开"))
            _state._pending.clear()
            _report_status(status=STATUS_DISCONNECTED)


def note_extension_error(detail: str) -> None:
    _report_status(detail, status=STATUS_ERROR)
