"""Minimal async CDP client over the browser's remote-debugging websocket.

Browser-companion-extension-design.md §7.1 (B 轨): Ginno launches a dedicated
real Chrome (``--user-data-dir`` + ``--remote-debugging-port``) and speaks CDP
directly — this module is that wire client. Only the domains the browser tools
need are implemented: targets/sessions, input, page lifecycle, screenshots,
runtime evaluation, DOM file inputs, and a passive console/network tap.

Concurrency model: one websocket, one reader task, command futures keyed by id.
Events fan out to per-session listeners (ring buffers, dialog auto-handling).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import urllib.request
from collections.abc import Callable
from typing import Any

from ..lang import t

log = logging.getLogger("ginno.browser.cdp")

_id_iter = itertools.count(1)

# Handlers: (connection, session_id, method, params) -> None (sync or async)
EventHandler = Callable[["CDPConnection", str, str, dict], Any]


class CDPError(RuntimeError):
    """CDP command error (response.error) — message is model-facing."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class CDPConnection:
    """A single browser-level DevTools websocket with flat sessions."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        # (session_id | "*") -> [handler]
        self._handlers: dict[str, list[EventHandler]] = {}
        self._closed = False

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    async def connect_port(cls, port: int, timeout_s: float = 10.0) -> CDPConnection:
        """Resolve the browser ws url from /json/version and connect."""
        url = f"http://127.0.0.1:{port}/json/version"
        loop = asyncio.get_running_loop()
        last_err: Exception | None = None
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    info = json.loads(r.read().decode())
                ws_url = info.get("webSocketDebuggerUrl")
                if ws_url:
                    conn = cls(ws_url)
                    await conn._open(timeout_s)
                    return conn
            except Exception as e:  # noqa: BLE001 — probe loop
                last_err = e
            await asyncio.sleep(0.25)
        raise CDPError(t(f"Cannot connect to the Chrome debug port {port}: {last_err}",
                         f"无法连接 Chrome 调试端口 {port}: {last_err}"))

    async def _open(self, timeout_s: float) -> None:
        import websockets  # uvicorn[standard] dependency, already bundled

        self._ws = await asyncio.wait_for(
            websockets.connect(self.ws_url, max_size=64 * 1024 * 1024), timeout_s
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CDPError("connection closed"))
        self._pending.clear()
        if self._reader:
            self._reader.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- events ----------------------------------------------------------

    def on(self, handler: EventHandler, session_id: str = "*") -> None:
        self._handlers.setdefault(session_id, []).append(handler)

    def off(self, handler: EventHandler, session_id: str = "*") -> None:
        lst = self._handlers.get(session_id)
        if lst and handler in lst:
            lst.remove(handler)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"] or {}
                            fut.set_exception(
                                CDPError(err.get("message", "CDP error"), err.get("code"))
                            )
                        else:
                            fut.set_result(msg.get("result"))
                    continue
                method = msg.get("method")
                if not method:
                    continue
                sid = msg.get("sessionId") or "*"
                for h in list(self._handlers.get(sid, [])) + list(
                    self._handlers.get("*", [])
                ):
                    try:
                        r = h(self, sid, method, msg.get("params") or {})
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:  # noqa: BLE001 — handlers must not kill the loop
                        log.exception("cdp event handler failed: %s", method)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("cdp read loop ended: %s", e)
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError("connection closed"))

    # ---- commands ---------------------------------------------------------

    async def send(self, method: str, params: dict | None = None,
                   session_id: str | None = None, timeout_s: float = 30.0) -> Any:
        if self._closed or self._ws is None:
            raise CDPError(t(
                "Browser connection lost; retry, or check that Chrome is running",
                "浏览器连接已断开,请重试或检查 Chrome 是否在运行"))
        mid = next(_id_iter)
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout_s)
        except TimeoutError:
            self._pending.pop(mid, None)
            raise CDPError(t(f"CDP command timed out: {method}",
                             f"CDP 命令超时: {method}")) from None

    # ---- targets / sessions ----------------------------------------------

    async def targets(self) -> list[dict]:
        out = await self.send("Target.getTargets")
        return [
            t.get("targetInfo") or t
            for t in (out or {}).get("targetInfos", [])
        ]

    async def new_target(self, url: str = "about:blank") -> dict:
        out = await self.send("Target.createTarget", {"url": url})
        return out or {}

    async def attach(self, target_id: str) -> str:
        out = await self.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        return (out or {}).get("sessionId") or ""

    async def close_target(self, target_id: str) -> None:
        await self.send("Target.closeTarget", {"targetId": target_id})


class TabSession:
    """One attached page target — the unit the browser tools operate on."""

    def __init__(self, conn: CDPConnection, target_id: str, session_id: str,
                 url: str = "", title: str = ""):
        self.conn = conn
        self.target_id = target_id
        self.session_id = session_id
        self.url = url
        self.title = title
        # ring buffers (design §4.4): attach 期间被动收集,读时才取
        self.console: list[dict] = []
        self.network: list[dict] = []
        self._origin = ""  # cross-domain navigation clears buffers
        # beforeunload policy for the next navigation: "dismiss" (default) or "accept"
        self.beforeunload_policy = "dismiss"
        self.beforeunload_result: dict | None = None

    async def cmd(self, method: str, params: dict | None = None,
                  timeout_s: float = 30.0) -> Any:
        return await self.conn.send(method, params, session_id=self.session_id,
                                    timeout_s=timeout_s)

    async def eval_js(self, expression: str, await_promise: bool = True,
                      timeout_s: float = 20.0) -> Any:
        """Evaluate in page context; returns the JSON value, raises CDPError
        with the page exception message on failure."""
        out = await self.cmd(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout_s=timeout_s,
        )
        if out is None:
            return None
        exc = out.get("exceptionDetails")
        if exc:
            desc = (exc.get("exception") or {}).get("description") or exc.get("text")
            raise CDPError(t(f"Page evaluation failed: {desc}",
                             f"页面执行失败: {desc}"))
        res = out.get("result") or {}
        if res.get("type") in ("object",) and res.get("subtype") == "null":
            return None
        return res.get("value")

    # ---- lifecycle ---------------------------------------------------------

    async def prepare(self) -> None:
        """Enable the domains the tools rely on; idempotent."""
        for m in ("Page.enable", "Runtime.enable", "Network.enable"):
            try:
                await self.cmd(m)
            except CDPError:
                pass

    async def inject_scripts(self, sources: list[str]) -> None:
        """Register bridge scripts on every future navigation + evaluate now."""
        for src in sources:
            try:
                await self.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": src})
            except CDPError:
                pass
            try:
                await self.eval_js(src, await_promise=False)
            except CDPError:
                pass

    async def wait_load(self, timeout_s: float = 10.0, poll_s: float = 0.1) -> bool:
        """Poll readyState (design §5.9: 100ms poll, bounded budget)."""
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                state = await self.eval_js("document.readyState", await_promise=False)
            except CDPError:
                state = None
            if state in ("complete", "interactive") and state == "complete":
                return True
            await asyncio.sleep(poll_s)
        return False
