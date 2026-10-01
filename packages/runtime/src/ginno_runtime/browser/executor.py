"""Browser backends: the executor abstraction behind the browser_* tools.

browser-companion-extension-design.md §7.1 — ``BrowserBackend`` is the
interface the tool layer talks to; two implementations:

- ``ProfileBackend`` (B 轨): Ginno launches a dedicated real Chrome
  (``--user-data-dir=~/.ginno/browser-profile``) and speaks CDP directly
  through :mod:`.cdp`. Zero install, isolated login state.
- ``RelayBackend`` (扩展轨): forwards tool invocations to the companion
  extension over the ``/extension`` WS relay (see :mod:`.relay`). Lives in
  the user's real browser with their logins.

Tool code never imports either directly — it goes through
:func:`get_backend` which prefers the extension when its connector is
connected and falls back per ``fallback_profile_mode``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import socket
import struct
import subprocess
import time
from typing import Any

from . import scripts
from .cdp import CDPConnection, CDPError, TabSession
from .config import BrowserConfig, load_browser_config, profile_dir

log = logging.getLogger("ginno.browser")

CONSOLE_CAP = 1000
NETWORK_CAP = 500
SCREENSHOT_QUALITY = {"low": 5, "medium": 40, "high": 60}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome(cfg: BrowserConfig | None = None) -> str | None:
    """Locate a Chrome-family executable (macOS first, then PATH)."""
    cfg = cfg or load_browser_config()
    if cfg.chrome_path:
        return cfg.chrome_path
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    for c in candidates:
        if shutil.which(c) or __import__("os").path.exists(c):
            return c
    for name in ("google-chrome", "chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Parse JPEG SOFn dimensions (screenshot pixel size, for coord mapping)."""
    i = 2
    n = len(data)
    while i + 8 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


class ProfileBackend:
    """B 轨: dedicated-profile Chrome driven over CDP."""

    name = "profile"

    def __init__(self, cfg: BrowserConfig | None = None):
        self.cfg = cfg or load_browser_config()
        self.proc: subprocess.Popen | None = None
        self.conn: CDPConnection | None = None
        self.tabs: dict[int, TabSession] = {}   # tab_id (target_id int suffix?) -> session
        self._tab_by_session: dict[str, int] = {}
        self._next_tab_id = 1
        self._lock = asyncio.Lock()      # serialize tool-level operations
        self._screenshot_ctx: dict[int, dict] = {}
        self._launch_dir = profile_dir()

    # ---- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self.conn and not self.conn.closed)

    async def ensure_running(self) -> None:
        async with self._lock:
            if self.connected:
                return
            chrome = find_chrome(self.cfg)
            if not chrome:
                raise CDPError(
                    "未找到 Chrome。请安装 Google Chrome,或在 设置 → 连接器 → Chrome "
                    "浏览器里填写 Chrome 可执行文件路径。"
                )
            port = _free_port()
            args = [
                chrome,
                "--user-data-dir=" + str(self._launch_dir),
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate",
                "--window-size=1440,900",
            ]
            if self.cfg.headless:
                args.append("--headless=new")
            args.append("about:blank")
            self.proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.conn = await CDPConnection.connect_port(port)
            self.conn.on(self._on_event)
            await self._adopt_targets()

    async def _adopt_targets(self) -> None:
        for t in await self.conn.targets():
            if t.get("type") == "page":
                await self._attach_target(t.get("targetId"), t.get("url", ""),
                                          t.get("title", ""))

    async def _attach_target(self, target_id: str, url: str, title: str) -> int:
        sid = await self.conn.attach(target_id)
        tab = TabSession(self.conn, target_id, sid, url, title)
        self.tabs[self._next_tab_id] = tab
        self._tab_by_session[sid] = self._next_tab_id
        tab_id = self._next_tab_id
        self._next_tab_id += 1
        await tab.prepare()
        await tab.inject_scripts(scripts.BRIDGE_SOURCES)
        return tab_id

    async def shutdown(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()
            self.proc = None
        self.tabs.clear()
        self._tab_by_session.clear()

    # ---- event tap: ring buffers + dialogs + buffer reset on nav -----------

    def _on_event(self, conn: CDPConnection, session_id: str, method: str,
                  params: dict) -> None:
        tab_id = self._tab_by_session.get(session_id)
        if tab_id is None:
            return
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        if method == "Runtime.consoleAPICalled":
            args = " ".join(
                str(a.get("value") if a.get("type") in ("string", "number", "boolean")
                    else a.get("description") or a.get("type") or "")
                for a in params.get("args", [])
            )
            tab.console.append({"type": params.get("type", "log"), "args": args,
                                "timestamp": params.get("timestamp", time.time() * 1000)})
            del tab.console[:-CONSOLE_CAP]
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails") or {}
            tab.console.append({
                "type": "exception",
                "args": (d.get("exception") or {}).get("description") or d.get("text") or "",
                "timestamp": time.time() * 1000,
            })
            del tab.console[:-CONSOLE_CAP]
        elif method == "Network.requestWillBeSent":
            tab.network.append({
                "requestId": params.get("requestId"),
                "method": (params.get("request") or {}).get("method", "GET"),
                "url": (params.get("request") or {}).get("url", ""),
                "type": params.get("type", "Other"),
                "timestamp": params.get("timestamp", time.time() * 1000),
                "status": None,
            })
            del tab.network[:-NETWORK_CAP]
        elif method == "Network.responseReceived":
            rid = params.get("requestId")
            for r in reversed(tab.network):
                if r.get("requestId") == rid and r.get("status") is None:
                    r["status"] = (params.get("response") or {}).get("status", 0)
                    break
        elif method == "Page.frameNavigated":
            # cross-domain navigation clears the debug buffers (design §4.4)
            url = (params.get("frame") or {}).get("url", "")
            if url:
                host = url.split("://", 1)[-1].split("/", 1)[0]
                if host and tab._origin and host != tab._origin:
                    tab.console.clear()
                    tab.network.clear()
                tab._origin = host
            self._screenshot_ctx.pop(tab_id, None)
        elif method == "Page.javascriptDialogOpening":
            self._handle_dialog(tab_id, params)

    def _handle_dialog(self, tab_id: int, params: dict) -> None:
        dtype = params.get("type", "alert")
        tab = self.tabs.get(tab_id)
        accept = True
        if dtype == "beforeunload":
            accept = bool(tab and tab.beforeunload_policy == "accept")
            if tab:
                tab.beforeunload_result = {"handled": True, "accepted": accept}
        async def _answer() -> None:
            try:
                await tab.cmd("Page.handleJavaScriptDialog", {"accept": accept})
            except CDPError:
                pass
        asyncio.ensure_future(_answer())

    # ---- tab API (what tools call) ------------------------------------------

    async def _tab(self, tab_id: int) -> TabSession:
        await self.ensure_running()
        tab = self.tabs.get(tab_id)
        if tab is None:
            raise CDPError(
                f"无效 tabId={tab_id}。先用 browser_tabs_context 获取当前可用标签。"
            )
        return tab

    async def tabs_context(self) -> list[dict]:
        await self.ensure_running()
        out = []
        for tid, tab in sorted(self.tabs.items()):
            try:
                info = await tab.eval_js(
                    "({url: location.href, title: document.title,"
                    " loading: document.readyState !== 'complete'})",
                    await_promise=False,
                )
            except CDPError:
                info = {"url": tab.url, "title": tab.title, "loading": False}
            out.append({"tabId": tid, **(info or {})})
        return out

    async def new_tab(self, url: str = "about:blank") -> dict:
        await self.ensure_running()
        async with self._lock:
            t = await self.conn.new_target(url)
            target_id = t.get("targetId", "")
            tab_id = await self._attach_target(target_id, url, "")
            if url != "about:blank":
                tab = self.tabs[tab_id]
                await tab.wait_load(self.cfg.load_timeout_s)
            return {"tabId": tab_id}

    async def close_tab(self, tab_id: int) -> dict:
        tab = await self.tabs.get(tab_id) or await self._tab(tab_id)
        await self.conn.close_target(tab.target_id)
        self.tabs.pop(tab_id, None)
        self._tab_by_session.pop(tab.session_id, None)
        self._screenshot_ctx.pop(tab_id, None)
        return {"closed": tab_id}

    # ---- navigation ----------------------------------------------------------

    async def history(self, tab_id: int, direction: str) -> dict:
        """back/forward with load wait; returns final url/title."""
        tab = await self._tab(tab_id)
        try:
            await tab.eval_js(f"history.{direction}()", await_promise=False)
        except CDPError:
            pass
        await tab.wait_load(self.cfg.load_timeout_s)
        info = await tab.eval_js(
            "({url: location.href, title: document.title})", await_promise=False
        ) or {}
        return info

    async def navigate(self, tab_id: int, url: str, force: bool = False) -> dict:
        tab = await self._tab(tab_id)
        tab.beforeunload_policy = "accept" if force else "dismiss"
        tab.beforeunload_result = None
        start = time.time()
        await tab.cmd("Page.navigate", {"url": url})
        await tab.wait_load(self.cfg.load_timeout_s)
        dur = round(time.time() - start, 1)
        res = tab.beforeunload_result or {}
        info = await tab.eval_js(
            "({url: location.href, title: document.title})", await_promise=False
        ) or {}
        return {"url": info.get("url", url), "title": info.get("title", ""),
                "durationS": dur, **res}

    # ---- input (trusted CDP events, design §5) --------------------------------

    async def _map_coords(self, tab_id: int, x: float, y: float) -> tuple[float, float]:
        ctx = self._screenshot_ctx.get(tab_id)
        if not ctx:
            return x, y
        sx = ctx.get("screenshotWidth") or ctx.get("viewportWidth") or 1
        sy = ctx.get("screenshotHeight") or ctx.get("viewportHeight") or 1
        return (x * ctx["viewportWidth"] / sx, y * ctx["viewportHeight"] / sy)

    async def click(self, tab_id: int, action: str, x: float, y: float,
                    modifiers: int = 0) -> dict:
        tab = await self._tab(tab_id)
        x, y = await self._map_coords(tab_id, x, y)
        button = "right" if action == "right_click" else "left"
        count = {"double_click": 2, "triple_click": 3}.get(action, 1)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", "x": x, "y": y, "modifiers": modifiers})
        await asyncio.sleep(0.05)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mousePressed", "x": x, "y": y, "button": button,
                       "clickCount": count, "modifiers": modifiers})
        await asyncio.sleep(0.05)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mouseReleased", "x": x, "y": y, "button": button,
                       "clickCount": count, "modifiers": modifiers})
        return {"action": action, "x": round(x), "y": round(y)}

    async def drag(self, tab_id: int, sx: float, sy: float, x: float, y: float,
                   modifiers: int = 0) -> dict:
        tab = await self._tab(tab_id)
        sx, sy = await self._map_coords(tab_id, sx, sy)
        x, y = await self._map_coords(tab_id, x, y)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", "x": sx, "y": sy, "modifiers": modifiers})
        await asyncio.sleep(0.05)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mousePressed", "x": sx, "y": sy, "button": "left",
                       "clickCount": 1, "modifiers": modifiers})
        await asyncio.sleep(0.05)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", "x": x, "y": y, "modifiers": modifiers})
        await asyncio.sleep(0.1)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                       "clickCount": 1, "modifiers": modifiers})
        return {"dragged": [round(sx), round(sy), round(x), round(y)]}

    async def hover(self, tab_id: int, x: float, y: float) -> dict:
        tab = await self._tab(tab_id)
        x, y = await self._map_coords(tab_id, x, y)
        await tab.cmd("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", "x": x, "y": y})
        return {"hover": [round(x), round(y)]}

    async def scroll(self, tab_id: int, direction: str, amount: int,
                     x: float | None = None, y: float | None = None) -> dict:
        tab = await self._tab(tab_id)
        x, y = await self._map_coords(tab_id, x or 640, y or 360)
        delta = {"up": (0, -100), "down": (0, 100),
                 "left": (-100, 0), "right": (100, 0)}[direction]
        try:
            await tab.cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y,
                "deltaX": delta[0] * amount, "deltaY": delta[1] * amount})
        except CDPError:
            # CDP → JS fallback (design §5.8)
            await tab.eval_js(
                "(function(){var d=document.scrollingElement||document.body;"
                f"d.scrollBy({{left:{delta[0] * amount},top:{delta[1] * amount},"
                "behavior:'instant'}});return true})()")
        await asyncio.sleep(0.2)
        return {"scrolled": direction, "amount": amount}

    async def type_text(self, tab_id: int, text: str, stop_check=None) -> dict:
        tab = await self._tab(tab_id)
        typed = 0
        for ch in text:
            if stop_check and stop_check():
                break
            if ch in "\n\r":
                await tab.cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
                await tab.cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
            else:
                code = ord(ch)
                await tab.cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": ch,
                    "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})
                await tab.cmd("Input.dispatchKeyEvent", {
                    "type": "char", "key": ch, "text": ch, "unmodifiedText": ch,
                    "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})
                await tab.cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": ch,
                    "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})
            await asyncio.sleep(0.02)
            typed += 1
        return {"typed": typed}

    KEY_MAP = {
        "return": "Enter", "enter": "Enter", "esc": "Escape", "escape": "Escape",
        "space": " ", "spacebar": " ", "tab": "Tab", "backspace": "Backspace",
        "delete": "Delete", "del": "Delete", "insert": "Insert", "ins": "Insert",
        "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
        "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
        "ctrl": "Control", "alt": "Alt", "shift": "Shift",
        "cmd": "Meta", "command": "Meta", "meta": "Meta",
    }
    VK_MAP = {
        "Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8, "Delete": 46,
        "Insert": 45, "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
        "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
        " ": 32, "Control": 17, "Alt": 18, "Shift": 16, "Meta": 91,
    }

    async def press_key(self, tab_id: int, text: str, repeat: int = 1,
                        stop_check=None) -> dict:
        tab = await self._tab(tab_id)
        parts = [p.strip() for p in text.split("+") if p.strip()]
        main = self.KEY_MAP.get(parts[-1].lower(), parts[-1])
        mods = 0
        for m in parts[:-1]:
            ml = m.lower()
            mods |= {"alt": 1, "ctrl": 2, "control": 2,
                     "meta": 4, "cmd": 4, "command": 4, "shift": 8}.get(ml, 0)
        vk = self.VK_MAP.get(main)
        n = 0
        for _ in range(max(1, min(repeat, 100))):
            if stop_check and stop_check():
                break
            base = {"key": main, "modifiers": mods}
            if vk:
                base.update({"windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            await tab.cmd("Input.dispatchKeyEvent", {"type": "keyDown", **base})
            await tab.cmd("Input.dispatchKeyEvent", {"type": "keyUp", **base})
            n += 1
        return {"pressed": text, "repeat": n}

    # ---- screenshots (quality ladder + fromSurface fallback, design §5.1) ----

    async def screenshot(self, tab_id: int, quality: str = "low",
                         region: list[float] | None = None) -> dict:
        tab = await self._tab(tab_id)
        q = SCREENSHOT_QUALITY.get(quality, 5)
        vp = await tab.eval_js(
            "({w: window.innerWidth, h: window.innerHeight,"
            " dpr: window.devicePixelRatio || 1})", await_promise=False
        ) or {"w": 1280, "h": 720, "dpr": 1}
        params: dict[str, Any] = {
            "format": "jpeg", "quality": q, "captureBeyondViewport": False}
        if region:
            x, y = await self._map_coords(tab_id, region[0], region[1])
            x2, y2 = await self._map_coords(tab_id, region[2], region[3])
            params["clip"] = {"x": min(x, x2), "y": min(y, y2),
                              "width": abs(x2 - x), "height": abs(y2 - y), "scale": 1}
        data = None
        for from_surface in (True, False):
            try:
                params["fromSurface"] = from_surface
                out = await tab.cmd("Page.captureScreenshot", params)
                data = (out or {}).get("data")
                if data:
                    break
            except CDPError:
                continue
        if not data:
            raise CDPError("截图失败:页面可能处于错误状态,请先 browser_navigate 到有效页面。")
        raw = base64.b64decode(data)
        w, h = _jpeg_size(raw) or (vp["w"], vp["h"])
        self._screenshot_ctx[tab_id] = {
            "viewportWidth": vp["w"], "viewportHeight": vp["h"],
            "screenshotWidth": w, "screenshotHeight": h,
        }
        return {"data": data, "mimeType": "image/jpeg",
                "width": w, "height": h,
                "note": f"Screenshot captured ({w}x{h}, quality={quality}, jpeg={q})"}

    # ---- semantic ops (bridge scripts) ----------------------------------------

    async def read_page(self, tab_id: int, mode: str = "all", depth: int = 15,
                        max_chars: int = 50000, ref_id: str | None = None) -> dict:
        tab = await self._tab(tab_id)
        out = await tab.eval_js(
            "globalThis.__ginnoAT && __ginnoAT.generate({mode: %r, depth: %d,"
            " max_chars: %d, ref_id: %r})"
            % (mode, depth, max_chars, ref_id or "")
        )
        if not out or "tree" not in out:
            raise CDPError("页面无障碍树不可用(可能还在加载)。稍等后重试,或先 browser_navigate。")
        return out

    async def find_elements(self, tab_id: int, query: str, max_results: int = 20) -> dict:
        tab = await self._tab(tab_id)
        out = await tab.eval_js(
            "globalThis.__ginnoBridge && __ginnoBridge.find(%r, %d)"
            % (query, max_results))
        return out or {"results": []}

    async def form_input(self, tab_id: int, ref: str, value: Any) -> dict:
        tab = await self._tab(tab_id)
        out = await tab.eval_js(
            "globalThis.__ginnoBridge && __ginnoBridge.fill(%r, %r)" % (ref, value))
        return out or {"success": False, "error": "bridge unavailable"}

    async def page_text(self, tab_id: int, max_chars: int = 50000) -> dict:
        tab = await self._tab(tab_id)
        out = await tab.eval_js(
            "globalThis.__ginnoBridge && __ginnoBridge.pageText(%d)" % max_chars)
        return out or {"error": "bridge unavailable"}

    async def eval_js(self, tab_id: int, code: str) -> Any:
        tab = await self._tab(tab_id)
        wrapped = (
            "(function(){try{return {ok: true, value: (function(){return (" + code +
            "\n)})()}}catch(e){try{" + code + "\n; return {ok: true, value: undefined}}"
            "catch(e2){return {ok: false, error: e2.message || String(e2)}}}})()"
        )
        out = await tab.eval_js(wrapped)
        if isinstance(out, dict) and out.get("ok") is False:
            raise CDPError("JavaScript 执行失败: " + str(out.get("error")))
        return (out or {}).get("value") if isinstance(out, dict) else out

    async def scroll_to(self, tab_id: int, ref: str) -> dict:
        tab = await self._tab(tab_id)
        out = await tab.eval_js(
            "globalThis.__ginnoAT && __ginnoAT.coords(%r, true)" % ref)
        if not out:
            raise CDPError(f"Element not found: {ref}. "
                           "Use browser_read_page or browser_find to get a fresh ref.")
        return out

    async def resolve_ref_coords(self, tab_id: int, ref: str) -> dict:
        return await self.scroll_to(tab_id, ref)

    # ---- debug buffers ---------------------------------------------------------

    async def console_messages(self, tab_id: int, pattern: str | None = None,
                               only_errors: bool = False, limit: int = 100) -> list[dict]:
        tab = await self._tab(tab_id)
        import re as _re
        rx = _re.compile(pattern, _re.I) if pattern else None
        rows = [c for c in tab.console
                if (not only_errors or c["type"] in ("error", "exception"))
                and (rx is None or rx.search(c["args"]))]
        return rows[-limit:]

    async def clear_console(self, tab_id: int) -> None:
        (await self._tab(tab_id)).console.clear()

    async def network_requests(self, tab_id: int, url_pattern: str | None = None,
                               limit: int = 100) -> list[dict]:
        tab = await self._tab(tab_id)
        rows = [r for r in tab.network
                if not url_pattern or url_pattern in r["url"]]
        return rows[-limit:]

    async def clear_network(self, tab_id: int) -> None:
        (await self._tab(tab_id)).network.clear()

    # ---- file upload (ref mode; design §4.5) -----------------------------------

    async def file_upload(self, tab_id: int, ref: str, paths: list[str],
                          trigger_ref: str | None = None) -> dict:
        tab = await self._tab(tab_id)
        attr = "data-ginno-file-ref"
        if not trigger_ref:
            out = await tab.eval_js(
                "globalThis.__ginnoAT && __ginnoAT.elInfo(%r)" % ref)
            info = out or {}
            if not info.get("found"):
                raise CDPError(f"Element not found: {ref}. "
                               "Use browser_read_page or browser_find to get a fresh ref.")
            if info.get("tagName") != "INPUT" or info.get("type") != "file":
                raise CDPError(
                    f'browser_file_upload 的 ref "{ref}" 解析为 {info.get("tagName")},'
                    "不是文件输入框。请用 browser_read_page 找到 input[type=file] 的 ref。")
        await tab.eval_js(
            "globalThis.__ginnoAT && __ginnoAT.markRef(%r, %r)"
            % (ref, attr))
        try:
            doc = await tab.cmd("DOM.getDocument")
            node_id = (await tab.cmd("DOM.querySelector", {
                "nodeId": doc["root"]["nodeId"],
                "selector": f'input[type="file"][{attr}="1"]',
            })).get("nodeId")
            if not node_id:
                raise CDPError("未能定位文件输入框的 DOM 节点。")
            await tab.cmd("DOM.setFileInputFiles", {"files": paths, "nodeId": node_id})
        finally:
            await tab.eval_js(
                "globalThis.__ginnoAT && __ginnoAT.clearMark(%r, %r)" % (ref, attr))
        # verify the page actually consumed the files
        await asyncio.sleep(0.3)
        check = await tab.eval_js(
            "(function(){var at=globalThis.__ginnoAT;var el=at&&at.resolve(%r);"
            "if(!el||el.tagName!=='INPUT'||el.type!=='file')return {ok:false,"
            "error:'input not found'};var names=(%r||[]).map(function(p){return "
            "String(p).split(/[\\\\/]/).pop()});var files=Array.from(el.files||[]);"
            "if(files.length!==names.length)return {ok:false,error:'期望 '+names.length+"
            "' 个文件,页面实际 '+files.length+' 个。请确认路径存在且为普通文件。'};"
            "return {ok:true,files:files.map(function(f){return {name:f.name,"
            "size:f.size,type:f.type||''}})}})()" % (ref, paths))
        if not check or not check.get("ok"):
            raise CDPError((check or {}).get("error", "文件选择校验失败"))
        return {"ok": True, "files": check.get("files", [])}

    async def resize_window(self, tab_id: int, width: int, height: int) -> dict:
        tab = await self._tab(tab_id)
        # B 轨 is our own window: use window.resizeTo (allowed for the
        # debugger-attached opener-less window Chrome flags us into).
        await tab.eval_js(f"window.resizeTo({width}, {height})", await_promise=False)
        self._screenshot_ctx.pop(tab_id, None)
        return {"resized": [width, height]}


# ---- module-level singleton + backend selection --------------------------------

_profile_backend: ProfileBackend | None = None


def get_profile_backend() -> ProfileBackend:
    global _profile_backend
    if _profile_backend is None:
        _profile_backend = ProfileBackend()
    return _profile_backend
