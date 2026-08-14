"""Browser engine abstraction.

``FakeEngine`` (in-memory pages, tests / no Chrome), ``ChromeEngine``
(system Chrome + isolated profile + CDP, **headless**), and ``CefEngine``
(packaged CEF tile + CDP, no screencast). ``try_cef()`` only returns an
instance when Helper.app is present **and** the host wrote a live CDP port
to ``~/.ginno/browser/cef-cdp.json``. Until then the page is painted into
BrowserPane via Chrome screencast frames.
The supervisor talks only to this interface so the CEF swap does not
change Space / ownership / REST.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import spaces as space_store

log = logging.getLogger(__name__)


def _svg_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class BrowserEngine(Protocol):
    def ensure_space(self, name: str) -> None: ...
    def navigate(self, name: str, url: str) -> dict[str, Any]: ...
    def snapshot(self, name: str) -> dict[str, Any]: ...
    def click(self, name: str, ref: str) -> dict[str, Any]: ...
    def fill(self, name: str, ref: str, value: str) -> dict[str, Any]: ...
    def hover(self, name: str, ref: str) -> dict[str, Any]: ...
    def select(self, name: str, ref: str, value: str) -> dict[str, Any]: ...
    def scroll(self, name: str, dx: int = 0, dy: int = 600) -> dict[str, Any]: ...
    def dispatch_key(self, name: str, key: str) -> dict[str, Any]: ...
    def upload_file(self, name: str, ref: str, path: str) -> dict[str, Any]: ...
    def evaluate(self, name: str, expression: str) -> Any: ...
    def element_eval(self, name: str, ref: str, expression: str) -> Any: ...
    def cdp(self, name: str, method: str, params: dict | None = None) -> Any: ...
    def screenshot(self, name: str) -> bytes: ...
    def page_info(self, name: str) -> dict[str, Any]: ...
    def list_tabs(self, name: str) -> list[dict[str, Any]]: ...
    def open_tab(self, name: str, url: str = "about:blank") -> dict[str, Any]: ...
    def close_tab(self, name: str, tab_id: str | None = None) -> dict[str, Any]: ...
    def switch_tab(self, name: str, tab_id: str) -> dict[str, Any]: ...
    def close_space(self, name: str) -> None: ...
    def close(self) -> None: ...
    def headed(self) -> bool: ...
    def activate(self, name: str) -> None: ...
    def set_bounds(self, x: int, y: int, width: int, height: int) -> dict[str, Any]: ...
    def set_viewport(self, name: str, width: int, height: int, dpr: float = 1.0) -> dict[str, Any]: ...
    def latest_frame(self, name: str) -> dict[str, Any]: ...
    def dispatch_input(self, name: str, event: dict[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Fake engine — in-memory pages, no Chrome. Default for tests / CI.
# --------------------------------------------------------------------------- #


@dataclass
class _FakePage:
    url: str = "about:blank"
    title: str = ""
    html: str = ""
    nodes: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)


class FakeEngine:
    """Deterministic in-process engine. Hosts a tiny login-wall fixture so the
    three-state ownership walk can run without Chrome."""

    kind = "fake"

    def __init__(self) -> None:
        self._pages: dict[str, _FakePage] = {}
        self._tabs: dict[str, list[dict[str, Any]]] = {}
        self._closed = False
        self._viewport: dict[str, int | float] = {"width": 800, "height": 600, "dpr": 1.0}

    def headed(self) -> bool:
        return False

    def ensure_space(self, name: str) -> None:
        self._pages.setdefault(name, _FakePage())
        if name not in self._tabs:
            page = self._pages[name]
            self._tabs[name] = [
                {"id": f"{name}:0", "url": page.url, "title": page.title, "active": True}
            ]

    def navigate(self, name: str, url: str) -> dict[str, Any]:
        self.ensure_space(name)
        page = self._pages[name]
        page.url = url
        if url.startswith("ginno://login-wall") or "login-wall" in url:
            page.title = "Login Wall"
            page.html = _LOGIN_WALL_HTML
            page.nodes = [
                {"ref": 1, "role": "heading", "name": "请先登录", "loc": "h1"},
                {"ref": 2, "role": "textbox", "name": "password", "loc": "#pw"},
                {"ref": 3, "role": "button", "name": "登录", "loc": "#login"},
            ]
            page.inputs = {}
        elif url.startswith("about:"):
            page.title = url
            page.html = f"<html><body>{url}</body></html>"
            page.nodes = [{"ref": 1, "role": "document", "name": url, "loc": "html"}]
        else:
            page.title = url.rsplit("/", 1)[-1] or url
            page.html = f"<html><body><a href='{url}'>{page.title}</a></body></html>"
            page.nodes = [
                {"ref": 1, "role": "link", "name": page.title, "loc": "a", "url": url},
            ]
        rec = next((t for t in self._tabs.get(name, []) if t.get("active")), None)
        if rec is None:
            self.ensure_space(name)
            rec = self._tabs[name][0]
        rec["url"] = page.url
        rec["title"] = page.title
        return self.page_info(name)

    def snapshot(self, name: str) -> dict[str, Any]:
        self.ensure_space(name)
        page = self._pages[name]
        lines = [f"[document] {page.title} — {page.url}"]
        for n in page.nodes:
            extra = ""
            if n["role"] == "textbox":
                extra = f" value={page.inputs.get(str(n['ref']), '')!r}"
            lines.append(
                f"  [ref={n['ref']}, loc={n.get('loc')!r}] [{n['role']}] {n.get('name','')}{extra}"
            )
        return {
            "url": page.url,
            "title": page.title,
            "text": "\n".join(lines),
            "refs": {str(n["ref"]): n for n in page.nodes},
        }

    def click(self, name: str, ref: str) -> dict[str, Any]:
        self.ensure_space(name)
        page = self._pages[name]
        node = next((n for n in page.nodes if str(n["ref"]) == str(ref).lstrip("@")), None)
        if not node:
            return {"ok": False, "error": f"unknown ref @{ref}"}
        if node.get("role") == "button" and node.get("name") == "登录":
            pw = page.inputs.get("2", "")
            if pw == "secret":
                page.title = "已登录"
                page.url = "ginno://login-wall/home"
                page.nodes = [
                    {"ref": 1, "role": "heading", "name": "欢迎回来", "loc": "h1"},
                    {"ref": 2, "role": "link", "name": "未读邮件 (3)", "loc": "a.inbox"},
                ]
                page.inputs = {}
                return {"ok": True, "action": "login", **self.page_info(name)}
            return {"ok": False, "error": "wrong password"}
        return {"ok": True, "clicked": node, **self.page_info(name)}

    def fill(self, name: str, ref: str, value: str) -> dict[str, Any]:
        self.ensure_space(name)
        page = self._pages[name]
        key = str(ref).lstrip("@")
        node = next((n for n in page.nodes if str(n["ref"]) == key), None)
        if not node:
            return {"ok": False, "error": f"unknown ref @{ref}"}
        page.inputs[key] = value
        return {"ok": True, "filled": key, "value": value}

    def evaluate(self, name: str, expression: str) -> Any:
        self.ensure_space(name)
        page = self._pages[name]
        # Tiny expression surface for tests: document.title / location.href.
        expr = (expression or "").strip()
        if expr in ("document.title", "title"):
            return page.title
        if expr in ("location.href", "document.URL"):
            return page.url
        if expr.startswith("return "):
            return None
        return None

    def page_info(self, name: str) -> dict[str, Any]:
        self.ensure_space(name)
        page = self._pages[name]
        return {"url": page.url, "title": page.title}

    def close_space(self, name: str) -> None:
        self._pages.pop(name, None)
        self._tabs.pop(name, None)

    def close(self) -> None:
        self._pages.clear()
        self._closed = True

    def hover(self, name: str, ref: str) -> dict[str, Any]:
        self.ensure_space(name)
        page = self._pages[name]
        node = next((n for n in page.nodes if str(n["ref"]) == str(ref).lstrip("@")), None)
        if not node:
            return {"ok": False, "error": f"unknown ref @{ref}"}
        return {"ok": True, "hovered": node}

    def select(self, name: str, ref: str, value: str) -> dict[str, Any]:
        return self.fill(name, ref, value)

    def scroll(self, name: str, dx: int = 0, dy: int = 600) -> dict[str, Any]:
        self.ensure_space(name)
        return {"ok": True, "dx": dx, "dy": dy}

    def dispatch_key(self, name: str, key: str) -> dict[str, Any]:
        self.ensure_space(name)
        return {"ok": True, "key": key}

    def upload_file(self, name: str, ref: str, path: str) -> dict[str, Any]:
        return self.fill(name, ref, path)

    def element_eval(self, name: str, ref: str, expression: str) -> Any:
        self.ensure_space(name)
        page = self._pages[name]
        node = next((n for n in page.nodes if str(n["ref"]) == str(ref).lstrip("@")), None)
        if not node:
            return None
        return node.get("name")

    def cdp(self, name: str, method: str, params: dict | None = None) -> Any:
        return {"ok": False, "error": "FakeEngine has no CDP", "method": method}

    def screenshot(self, name: str) -> bytes:
        self.ensure_space(name)
        # 1x1 PNG so callers can persist a file without Chrome.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00"
            b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    def list_tabs(self, name: str) -> list[dict[str, Any]]:
        self.ensure_space(name)
        return [dict(t) for t in self._tabs.get(name, [])]

    def open_tab(self, name: str, url: str = "about:blank") -> dict[str, Any]:
        self.ensure_space(name)
        for t in self._tabs[name]:
            t["active"] = False
        tid = f"{name}:{len(self._tabs[name])}"
        rec = {"id": tid, "url": url or "about:blank", "title": "", "active": True}
        self._tabs[name].append(rec)
        self.navigate(name, url or "about:blank")
        rec["url"] = self._pages[name].url
        rec["title"] = self._pages[name].title
        return rec

    def close_tab(self, name: str, tab_id: str | None = None) -> dict[str, Any]:
        self.ensure_space(name)
        tabs = self._tabs[name]
        if len(tabs) <= 1:
            return {"ok": False, "error": "last tab"}
        tid = tab_id or next((t["id"] for t in tabs if t.get("active")), tabs[-1]["id"])
        kept = [t for t in tabs if t.get("id") != tid]
        if len(kept) == len(tabs):
            return {"ok": False, "error": "no such tab"}
        if not any(t.get("active") for t in kept):
            kept[-1]["active"] = True
            self._pages[name].url = kept[-1].get("url") or "about:blank"
            self._pages[name].title = kept[-1].get("title") or ""
        self._tabs[name] = kept
        return {"ok": True, "tabs": self.list_tabs(name)}

    def switch_tab(self, name: str, tab_id: str) -> dict[str, Any]:
        self.ensure_space(name)
        hit = None
        for t in self._tabs[name]:
            t["active"] = t.get("id") == tab_id
            if t["active"]:
                hit = t
        if hit is None:
            return {"ok": False, "error": f"no such tab {tab_id!r}"}
        page = self._pages[name]
        page.url = hit.get("url") or page.url
        page.title = hit.get("title") or page.title
        return self.page_info(name)

    def set_bounds(self, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        return {"ok": True, "note": "fake", "bounds": {"x": x, "y": y, "width": width, "height": height}}

    def set_viewport(self, name: str, width: int, height: int, dpr: float = 1.0) -> dict[str, Any]:
        if name:
            self.ensure_space(name)
        self._viewport = {"width": int(width), "height": int(height), "dpr": float(dpr)}
        return {"ok": True, **self._viewport}

    def latest_frame(self, name: str) -> dict[str, Any]:
        """SVG stand-in so the pane still paints without Chrome."""
        self.ensure_space(name)
        page = self._pages[name]
        w = int((getattr(self, "_viewport", None) or {}).get("width") or 800)
        h = int((getattr(self, "_viewport", None) or {}).get("height") or 600)
        w, h = max(320, min(w, 1600)), max(240, min(h, 1200))
        rows = []
        y = 72
        for n in page.nodes:
            label = f"[{n.get('role')}] {n.get('name') or ''}"
            rows.append(
                f'<text x="28" y="{y}" fill="#d4d4d8" font-size="14" '
                f'font-family="ui-sans-serif,system-ui">{_svg_escape(label)}</text>'
            )
            y += 28
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
            f'<rect width="100%" height="100%" fill="#18181b"/>'
            f'<text x="28" y="36" fill="#a1a1aa" font-size="12" font-family="ui-monospace,monospace">'
            f"{_svg_escape(page.url or 'about:blank')}</text>"
            f'<text x="28" y="56" fill="#fafafa" font-size="18" font-family="ui-sans-serif,system-ui">'
            f"{_svg_escape(page.title or name)}</text>"
            + "".join(rows)
            + "</svg>"
        )
        return {
            "bytes": svg.encode("utf-8"),
            "mime": "image/svg+xml",
            "width": w,
            "height": h,
        }

    def dispatch_input(self, name: str, event: dict[str, Any]) -> dict[str, Any]:
        self.ensure_space(name)
        kind = (event or {}).get("type") or ""
        page = self._pages[name]
        # Map a click on the fake login wall onto the password / login controls
        # so the in-pane handoff walk still works without Chrome.
        if kind in ("mousePressed", "mouseReleased") and page.nodes:
            y = float((event or {}).get("y") or 0)
            if y >= 100:
                btn = next((n for n in page.nodes if n.get("role") == "button"), None)
                if btn and kind == "mousePressed":
                    return self.click(name, str(btn["ref"]))
            box = next((n for n in page.nodes if n.get("role") == "textbox"), None)
            if box:
                return {"ok": True, "focused": box["ref"]}
        if kind in ("keyDown", "keyUp") and page.nodes:
            box = next((n for n in page.nodes if n.get("role") == "textbox"), None)
            if box and kind == "keyDown":
                ch = str((event or {}).get("text") or (event or {}).get("key") or "")
                if len(ch) == 1:
                    key = str(box["ref"])
                    page.inputs[key] = page.inputs.get(key, "") + ch
                    return {"ok": True, "filled": key}
        return {"ok": True, "type": kind}

    def activate(self, name: str) -> None:
        self.ensure_space(name)


_LOGIN_WALL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Login Wall</title></head>
<body>
  <h1>请先登录</h1>
  <form id="f" onsubmit="return false">
    <label>密码 <input id="pw" type="password"></label>
    <button id="login" type="button">登录</button>
  </form>
  <p id="hint">正确密码是 secret。handoff 后来这里亲手输入。</p>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Chrome engine — system Chrome, isolated user-data-dir, headless + CDP.
# The OS window never opens. Page.screencastFrame paints into BrowserPane;
# Input.dispatch* forwards clicks/keys from the pane onto the real page.
# --------------------------------------------------------------------------- #


def _find_chrome() -> str | None:
    env = os.environ.get("GINNO_CHROME_BIN")
    if env and os.path.exists(env):
        return env
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def login_wall_path() -> Path | None:
    """Locate the headed login-wall fixture (source tree, wheel, or frozen app)."""
    import sys

    here = Path(__file__).resolve().parent
    candidates = [
        here / "fixtures" / "login-wall.html",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(
            Path(meipass) / "ginno_runtime" / "browser" / "fixtures" / "login-wall.html"
        )
    # Source-tree fallback used by `uv run` from packages/runtime.
    candidates.append(here.parents[2] / "tests" / "fixtures" / "login-wall.html")
    for p in candidates:
        if p.is_file():
            return p
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_CDP_ALLOW = (
    "Page.",
    "Runtime.",
    "Input.",
    "DOM.",
    "Accessibility.",
    "Network.get",
    "Emulation.",
    "Overlay.",
    "Target.get",
    "Target.activate",
    "Target.createTarget",
    "Target.closeTarget",
    "CSS.get",
)
_CDP_DENY = (
    "Browser.close",
    "Browser.crash",
    "Browser.setPermission",
    "Target.createBrowserContext",
    "Target.disposeBrowserContext",
    "SystemInfo.",
)


def _reap_stale_chrome(profile: Path) -> None:
    """Kill leftover Ginno Chrome holding ``profile`` (headed leftover / crash).

    Never touches the user's everyday Chrome (different user-data-dir).
    """
    try:
        import subprocess

        r = subprocess.run(
            ["pgrep", "-f", f"--user-data-dir={profile}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        r = None
    pids: list[int] = []
    if r and r.returncode == 0:
        for line in (r.stdout or "").split():
            try:
                pids.append(int(line))
            except ValueError:
                continue
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        except Exception:
            log.debug("reap chrome pid %s failed", pid, exc_info=True)
    if pids:
        time.sleep(0.4)
        for pid in pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            except Exception:
                pass
    # Chrome leaves Singleton* even after a crash; a stale lock blocks the next launch.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile / name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except Exception:
            log.debug("could not drop %s", lock, exc_info=True)


class ChromeEngine:
    """Headless Chrome with ``--user-data-dir=~/.ginno/browser/profile``.

    One Chrome process, many tabs (one or more per Space). The page is painted
    into Ginno via CDP screencast — no OS window, no AppleScript dock.
    """

    kind = "chrome"

    def __init__(
        self,
        binary: str | None = None,
        *,
        attach_port: int | None = None,
        screencast: bool = True,
    ) -> None:
        self._attach = attach_port is not None
        self._screencast = bool(screencast) and not self._attach
        self.binary = binary or (None if self._attach else _find_chrome())
        if not self._attach and not self.binary:
            raise RuntimeError("no Chrome/Chromium binary found")
        space_store.ensure_browser_layout()
        self._port = int(attach_port) if attach_port else _free_port()
        self._proc: subprocess.Popen | None = None
        self._tabs: dict[str, str] = {}  # space name → active CDP target id
        self._space_tabs: dict[str, list[str]] = {}  # space name → owned target ids
        self._sessions: dict[str, Any] = {}  # target id → CdpSession
        self._refs: dict[str, dict[str, dict[str, Any]]] = {}  # space → refMap
        self._frames: dict[str, dict[str, Any]] = {}  # space → latest jpeg
        # CSS viewport the pane last reported. Input.dispatch* uses this space.
        self._viewport: dict[str, int | float] = {"width": 1024, "height": 768, "dpr": 1.0}
        self._cast_lock = threading.Lock()
        self._dl_lock = threading.Lock()
        self._dl_seen: set[str] = set()
        if self._attach:
            self._wait_cdp()
        else:
            _reap_stale_chrome(space_store.profile_dir())
            self._start()

    def headed(self) -> bool:
        # Attached CEF paints natively. Spawned Chrome is headless screencast.
        return bool(self._attach)

    def _start(self) -> None:
        args = [
            self.binary,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={space_store.profile_dir()}",
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=Translate",
            # Initial only; Emulation.setDeviceMetricsOverride follows the pane.
            "--window-size=1280,800",
            "--force-device-scale-factor=1",
            "about:blank",
        ]
        log.info("chrome launch port=%s profile=%s", self._port, space_store.profile_dir())
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 15
        last_err = None
        while time.time() < deadline:
            try:
                self._http("/json/version")
                return
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        raise RuntimeError(f"chrome CDP did not come up: {last_err}")

    def _wait_cdp(self) -> None:
        deadline = time.time() + 8
        last_err = None
        while time.time() < deadline:
            try:
                self._http("/json/version")
                return
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        raise RuntimeError(f"CEF CDP did not come up: {last_err}")

    def _http(self, path: str, payload: dict | None = None, *, method: str | None = None) -> Any:
        url = f"http://127.0.0.1:{self._port}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        # Chrome DevTools HTTP: GET /json/list, PUT /json/new, GET /json/activate.
        # urllib defaults to GET (no body) / POST (body). /json/new is PUT-only
        # on current Chrome — a GET returns HTTP 405 and the Space stays blank.
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None

    def _targets(self) -> list[dict]:
        try:
            data = self._http("/json/list")
        except Exception:
            data = self._http("/json")
        return data if isinstance(data, list) else []

    def _page_targets(self) -> list[dict]:
        return [t for t in self._targets() if t.get("type") == "page"]

    def _ws_url_for(self, tid: str) -> str | None:
        for t in self._page_targets():
            if t.get("id") == tid:
                return t.get("webSocketDebuggerUrl")
        return None

    def _session(self, name: str):
        from .cdp import CdpSession

        tid = self._activate(name)
        if not tid:
            raise RuntimeError(f"no chrome tab for space {name!r}")
        sess = self._sessions.get(tid)
        if sess is not None and sess.alive():
            return sess
        ws = self._ws_url_for(tid)
        if not ws:
            raise RuntimeError(f"no debugger url for tab {tid}")
        sess = CdpSession(ws)
        sess.connect()
        try:
            sess.call("Page.enable")
            sess.call("Runtime.enable")
            sess.call("DOM.enable")
            sess.call("Accessibility.enable")
        except Exception:
            log.debug("cdp domain enable failed", exc_info=True)
        self._enable_downloads(sess)
        self._sessions[tid] = sess
        self._apply_metrics(sess)
        self._start_screencast(name, sess)
        return sess

    def _drop_session(self, tid: str | None) -> None:
        if not tid:
            return
        sess = self._sessions.pop(tid, None)
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    def _create_target(self) -> str | None:
        """Open a fresh about:blank tab. Chrome's HTTP /json/new is PUT-only."""
        last_err: Exception | None = None
        try:
            created = self._http("/json/new?about:blank", method="PUT")
            if isinstance(created, dict) and created.get("id"):
                return str(created["id"])
        except Exception as e:
            last_err = e
            log.info("chrome PUT /json/new failed: %s", e)
        # Fallback: Target.createTarget over any live CDP session (or a
        # throwaway attach to the first page). Avoids GET /json/new 405.
        try:
            from .cdp import CdpSession

            sess = next((s for s in self._sessions.values() if s is not None and s.alive()), None)
            owned = sess is None
            if sess is None:
                pages = self._page_targets()
                ws = (pages[0].get("webSocketDebuggerUrl") if pages else None) or None
                if not ws:
                    raise RuntimeError("no existing page to attach for Target.createTarget")
                sess = CdpSession(ws)
                sess.connect()
            try:
                out = sess.call("Target.createTarget", {"url": "about:blank"})
                tid = (out or {}).get("targetId") or (out or {}).get("id")
                if tid:
                    return str(tid)
            finally:
                if owned:
                    try:
                        sess.close()
                    except Exception:
                        pass
        except Exception as e:
            last_err = e
            log.exception("chrome Target.createTarget failed")
        if last_err is not None:
            raise RuntimeError(f"chrome could not open a new tab: {last_err}") from last_err
        raise RuntimeError("chrome could not open a new tab")

    def ensure_space(self, name: str) -> None:
        if name in self._tabs:
            tid = self._tabs[name]
            if any(t.get("id") == tid for t in self._page_targets()):
                self._claim_tab(name, tid)
                return
            self._drop_session(tid)
            self._tabs.pop(name, None)
        pages = self._page_targets()
        claimed = set(self._tabs.values())
        unused = [t for t in pages if t.get("id") not in claimed]
        # Only adopt a leftover about:blank. Never steal another Space's live tab
        # (that is how /browse ended up painting the wrong page / staying blank).
        blank = next(
            (
                t
                for t in unused
                if (t.get("url") or "").startswith("about:blank") or not (t.get("url") or "")
            ),
            None,
        )
        if blank and blank.get("id"):
            self._claim_tab(name, str(blank["id"]))
            return
        tid = self._create_target()
        if tid:
            self._claim_tab(name, tid)
            return
        raise RuntimeError(f"chrome could not open a tab for space {name!r}")

    def _claim_tab(self, name: str, tid: str) -> None:
        self._tabs[name] = tid
        owned_map = getattr(self, "_space_tabs", None)
        if owned_map is None:
            self._space_tabs = {}
            owned_map = self._space_tabs
        owned = owned_map.setdefault(name, [])
        if tid not in owned:
            owned.append(tid)

    def _activate(self, name: str) -> str | None:
        self.ensure_space(name)
        tid = self._tabs.get(name)
        if not tid:
            return None
        try:
            self._http(f"/json/activate/{tid}")
        except Exception:
            pass
        return tid

    def _resolve_url(self, url: str) -> str:
        from .helpers import normalize_url

        raw = normalize_url(url)
        if raw.startswith("ginno://login-wall"):
            fixture = login_wall_path()
            if fixture is not None:
                return fixture.as_uri()
        return raw

    def navigate(self, name: str, url: str) -> dict[str, Any]:
        target = self._resolve_url(url)
        try:
            sess = self._session(name)
            sess.call("Page.navigate", {"url": target})
            try:
                sess.wait_event("Page.loadEventFired", timeout=20)
            except Exception:
                time.sleep(0.4)
        except Exception:
            log.exception("chrome navigate failed")
            raise
        self._refs.pop(name, None)
        return self.page_info(name)

    def snapshot(self, name: str) -> dict[str, Any]:
        from .snapshot import flatten_ax, format_snapshot, merge_ax_frames

        info = self.page_info(name)
        try:
            sess = self._session(name)
            tree = sess.call("Accessibility.getFullAXTree", timeout=15)
        except Exception as e:
            log.debug("ax tree failed: %s", e)
            text = format_snapshot(info.get("url") or "", info.get("title") or "", "")
            return {**info, "text": text + "\n  (accessibility tree unavailable)", "refs": {}}
        frames: list[tuple[str, dict | None]] = [("", tree)]
        try:
            frames.extend(self._same_origin_iframe_trees(sess, tree))
        except Exception:
            log.debug("iframe ax walk failed", exc_info=True)
        try:
            shadow = self._open_shadow_note(sess)
            if shadow:
                frames.append((shadow, None))
        except Exception:
            log.debug("open-shadow probe failed", exc_info=True)
        if len(frames) == 1:
            body, refs = flatten_ax(tree)
        else:
            body, refs = merge_ax_frames(frames)
        self._refs[name] = refs
        text = format_snapshot(info.get("url") or "", info.get("title") or "", body)
        return {**info, "text": text, "refs": refs}

    def _same_origin_iframe_trees(self, sess: Any, main_tree: dict) -> list[tuple[str, dict | None]]:
        """Walk Page.getFrameTree and pull AX for same-origin child frames."""
        from .snapshot import flatten_frame_tree

        try:
            raw = sess.call("Page.getFrameTree", timeout=8)
        except Exception:
            return []
        tree = raw.get("frameTree") if isinstance(raw, dict) else raw
        frames = flatten_frame_tree(tree if isinstance(tree, dict) else {})
        if len(frames) <= 1:
            return []
        main = frames[0]
        main_origin = _origin_of(main.get("url") or "")
        main_id = main.get("id")
        out: list[tuple[str, dict | None]] = []
        for fr in frames[1:]:
            fid = fr.get("id")
            url = fr.get("url") or ""
            label = fr.get("name") or url or str(fid or "iframe")
            if not fid or fid == main_id:
                continue
            if _origin_of(url) != main_origin or not url or url.startswith("about:"):
                out.append((label, None))
                continue
            try:
                child = sess.call(
                    "Accessibility.getFullAXTree",
                    {"frameId": fid},
                    timeout=10,
                )
            except Exception:
                out.append((label, None))
                continue
            out.append((label, child if isinstance(child, dict) else None))
        return out

    def _open_shadow_note(self, sess: Any) -> str:
        """Mention open shadow roots so the agent knows they exist.

        Closed shadow stays omitted (M1 honesty). Open shadow hosts are
        already in the AX tree when Chrome exposes them; this is a hint
        when the probe finds hosts the AX walk missed.
        """
        expr = (
            "(function(){try{var n=0,w=document.createTreeWalker("
            "document,NodeFilter.SHOW_ELEMENT);var e;"
            "while(e=w.nextNode()){if(e.shadowRoot) n++;}return n;}catch(x){return 0;}})()"
        )
        try:
            out = sess.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            n = int(((out.get("result") or {}).get("value")) or 0)
        except Exception:
            return ""
        if n <= 0:
            return ""
        return f"{n} open shadow root(s) (in AX when exposed)"

    def _resolve_ref(self, name: str, ref: str) -> dict[str, Any] | None:
        key = str(ref).lstrip("@")
        rec = (self._refs.get(name) or {}).get(key)
        if rec:
            return rec
        # Refresh once — @N is only valid for the last snapshot, but a stale
        # map after navigate is a common agent mistake.
        try:
            self.snapshot(name)
        except Exception:
            return None
        return (self._refs.get(name) or {}).get(key)

    def _box_center(self, sess, backend_id: int) -> tuple[float, float] | None:
        try:
            resolved = sess.call("DOM.resolveNode", {"backendNodeId": int(backend_id)})
            obj_id = (resolved.get("object") or {}).get("objectId")
            if not obj_id:
                return None
            box = sess.call("DOM.getBoxModel", {"objectId": obj_id})
            content = (box.get("model") or {}).get("content") or []
            if len(content) < 8:
                return None
            xs = content[0::2]
            ys = content[1::2]
            return (sum(xs) / 4.0, sum(ys) / 4.0)
        except Exception:
            return None

    def click(self, name: str, ref: str) -> dict[str, Any]:
        node = self._resolve_ref(name, ref)
        if not node:
            return {"ok": False, "error": f"unknown ref @{str(ref).lstrip('@')} — snapshot again"}
        bid = node.get("backendNodeId")
        if not bid:
            return {"ok": False, "error": f"ref @{node.get('ref')} has no backendNodeId"}
        try:
            sess = self._session(name)
            center = self._box_center(sess, int(bid))
            if center is None:
                sess.call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": int(bid)})
                center = self._box_center(sess, int(bid))
            if center is None:
                return {"ok": False, "error": "could not resolve box for click"}
            x, y = center
            sess.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            sess.call(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
            sess.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
        except Exception as e:
            return {"ok": False, "error": f"click failed: {e}"}
        time.sleep(0.15)
        return {"ok": True, "clicked": node, **self.page_info(name)}

    def fill(self, name: str, ref: str, value: str) -> dict[str, Any]:
        clicked = self.click(name, ref)
        if not clicked.get("ok"):
            return clicked
        try:
            sess = self._session(name)
            # Select-all then type so we replace, not append.
            sess.call(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": "a", "modifiers": 2, "windowsVirtualKeyCode": 65},
            )
            sess.call(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": "a", "modifiers": 2, "windowsVirtualKeyCode": 65},
            )
            for ch in str(value):
                sess.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch})
                sess.call("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch, "key": ch})
        except Exception as e:
            return {"ok": False, "error": f"fill failed: {e}"}
        return {"ok": True, "filled": str(ref).lstrip("@"), "value": value}

    def hover(self, name: str, ref: str) -> dict[str, Any]:
        node = self._resolve_ref(name, ref)
        if not node or not node.get("backendNodeId"):
            return {"ok": False, "error": f"unknown ref @{ref}"}
        try:
            sess = self._session(name)
            center = self._box_center(sess, int(node["backendNodeId"]))
            if not center:
                return {"ok": False, "error": "no box"}
            sess.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": center[0], "y": center[1]})
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "hovered": node}

    def select(self, name: str, ref: str, value: str) -> dict[str, Any]:
        node = self._resolve_ref(name, ref)
        if not node or not node.get("backendNodeId"):
            return {"ok": False, "error": f"unknown ref @{ref}"}
        expr = (
            "(function(el, v){ if(!el) return false; el.value = v; "
            "el.dispatchEvent(new Event('input',{bubbles:true})); "
            "el.dispatchEvent(new Event('change',{bubbles:true})); return true; })"
        )
        try:
            sess = self._session(name)
            resolved = sess.call("DOM.resolveNode", {"backendNodeId": int(node["backendNodeId"])})
            obj_id = (resolved.get("object") or {}).get("objectId")
            if not obj_id:
                return {"ok": False, "error": "resolveNode failed"}
            sess.call(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": expr,
                    "objectId": obj_id,
                    "arguments": [{"value": value}],
                    "returnByValue": True,
                },
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "selected": value}

    def scroll(self, name: str, dx: int = 0, dy: int = 600) -> dict[str, Any]:
        try:
            sess = self._session(name)
            sess.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": 200, "y": 200, "deltaX": dx, "deltaY": dy},
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "dx": dx, "dy": dy}

    def dispatch_key(self, name: str, key: str) -> dict[str, Any]:
        try:
            sess = self._session(name)
            sess.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "text": key if len(key) == 1 else ""})
            sess.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "key": key}

    def upload_file(self, name: str, ref: str, path: str) -> dict[str, Any]:
        node = self._resolve_ref(name, ref)
        if not node or not node.get("backendNodeId"):
            return {"ok": False, "error": f"unknown ref @{ref}"}
        p = Path(path).expanduser()
        if not p.is_file():
            return {"ok": False, "error": f"file not found: {path}"}
        try:
            sess = self._session(name)
            sess.call("DOM.setFileInputFiles", {"backendNodeId": int(node["backendNodeId"]), "files": [str(p)]})
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "uploaded": str(p)}

    def evaluate(self, name: str, expression: str) -> Any:
        expr = (expression or "").strip()
        if not expr:
            return None
        # Allow `return x` from helper scripts.
        if expr.startswith("return "):
            expr = f"(function(){{ {expr} }})()"
        try:
            sess = self._session(name)
            result = sess.call(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": True},
                timeout=20,
            )
        except Exception as e:
            return {"error": str(e)}
        if result.get("exceptionDetails"):
            det = result["exceptionDetails"]
            return {"error": det.get("text") or str(det)}
        val = (result.get("result") or {}).get("value")
        return val

    def element_eval(self, name: str, ref: str, expression: str) -> Any:
        node = self._resolve_ref(name, ref)
        if not node or not node.get("backendNodeId"):
            return None
        body = (expression or "el => el.textContent") .strip()
        try:
            sess = self._session(name)
            resolved = sess.call("DOM.resolveNode", {"backendNodeId": int(node["backendNodeId"])})
            obj_id = (resolved.get("object") or {}).get("objectId")
            if not obj_id:
                return None
            fn = body if body.startswith("function") or "=>" in body else f"function(el){{ return ({body}); }}"
            out = sess.call(
                "Runtime.callFunctionOn",
                {"functionDeclaration": fn, "objectId": obj_id, "returnByValue": True},
            )
            return (out.get("result") or {}).get("value")
        except Exception as e:
            return {"error": str(e)}

    def cdp(self, name: str, method: str, params: dict | None = None) -> Any:
        method = (method or "").strip()
        if not method:
            return {"ok": False, "error": "cdp method required"}
        if any(method.startswith(d) or method == d.rstrip(".") for d in _CDP_DENY):
            return {"ok": False, "error": f"cdp method {method!r} is denied"}
        if not any(method.startswith(a) for a in _CDP_ALLOW):
            return {"ok": False, "error": f"cdp method {method!r} is not on the allow-list"}
        try:
            sess = self._session(name)
            return {"ok": True, "result": sess.call(method, params or {})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def screenshot(self, name: str) -> bytes:
        import base64

        sess = self._session(name)
        out = sess.call("Page.captureScreenshot", {"format": "png"}, timeout=20)
        raw = out.get("data") or ""
        return base64.b64decode(raw)

    def page_info(self, name: str) -> dict[str, Any]:
        tid = self._activate(name)
        for t in self._page_targets():
            if t.get("id") == tid:
                return {"url": t.get("url") or "", "title": t.get("title") or ""}
        try:
            sess = self._session(name)
            url = sess.call("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})
            title = sess.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
            return {
                "url": ((url.get("result") or {}).get("value")) or "",
                "title": ((title.get("result") or {}).get("value")) or "",
            }
        except Exception:
            return {"url": "", "title": ""}

    def _owned_tabs(self, name: str) -> list[str]:
        owned_map = getattr(self, "_space_tabs", None)
        if owned_map is None:
            self._space_tabs = {}
            owned_map = self._space_tabs
        return list(owned_map.get(name) or [])

    def list_tabs(self, name: str) -> list[dict[str, Any]]:
        """Only the tabs this Space owns — never every Chrome page target."""
        self.ensure_space(name)
        active = self._tabs.get(name)
        owned = self._owned_tabs(name)
        if active and active not in owned:
            owned.append(active)
            self._space_tabs[name] = owned
        by_id = {t.get("id"): t for t in self._page_targets()}
        out = []
        still = []
        for tid in owned:
            t = by_id.get(tid)
            if not t:
                continue
            still.append(tid)
            out.append(
                {
                    "id": tid,
                    "url": t.get("url") or "",
                    "title": t.get("title") or "",
                    "active": tid == active,
                }
            )
        self._space_tabs[name] = still
        if not out and active:
            t = by_id.get(active) or {}
            out.append(
                {
                    "id": active,
                    "url": t.get("url") or "",
                    "title": t.get("title") or "",
                    "active": True,
                }
            )
        return out

    def open_tab(self, name: str, url: str = "about:blank") -> dict[str, Any]:
        self.ensure_space(name)
        tid = self._create_target()
        if not tid:
            return {"ok": False, "error": "could not open tab"}
        self._claim_tab(name, tid)
        if url and url not in ("about:blank",):
            try:
                self.navigate(name, url)
            except Exception as e:
                return {"ok": False, "error": str(e), "id": tid}
        info = self.page_info(name)
        return {"id": tid, "url": info.get("url") or url, "title": info.get("title") or "", "active": True}

    def close_tab(self, name: str, tab_id: str | None = None) -> dict[str, Any]:
        owned = self._owned_tabs(name)
        tid = tab_id or self._tabs.get(name)
        if not tid:
            return {"ok": False, "error": "no tab"}
        if owned and tid not in owned:
            return {"ok": False, "error": "tab is not in this space"}
        if len(owned) <= 1:
            return {"ok": False, "error": "last tab"}
        self._drop_session(tid)
        try:
            self._http(f"/json/close/{tid}")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        kept = [x for x in owned if x != tid]
        self._space_tabs[name] = kept
        if self._tabs.get(name) == tid:
            nxt = kept[-1] if kept else None
            if nxt:
                self._tabs[name] = nxt
                self._activate(name)
            else:
                self._tabs.pop(name, None)
        return {"ok": True, "tabs": self.list_tabs(name)}

    def switch_tab(self, name: str, tab_id: str) -> dict[str, Any]:
        owned = self._owned_tabs(name)
        if owned and tab_id not in owned:
            return {"ok": False, "error": "tab is not in this space"}
        self._tabs[name] = tab_id
        self._activate(name)
        return self.page_info(name)

    def close_space(self, name: str) -> None:
        owned_map = getattr(self, "_space_tabs", None) or {}
        owned = list(owned_map.pop(name, []) or [])
        active = self._tabs.pop(name, None)
        if active and active not in owned:
            owned.append(active)
        self._refs.pop(name, None)
        for tid in owned:
            self._drop_session(tid)
            try:
                self._http(f"/json/close/{tid}")
            except Exception:
                pass

    def close(self) -> None:
        for tid in list(self._sessions):
            self._drop_session(tid)
        if self._attach:
            self._proc = None
            return
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def activate(self, name: str) -> None:
        """Select the Space tab. No OS window — the pane is the surface."""
        self._activate(name)
        try:
            self._start_screencast(name, self._session(name))
        except Exception:
            log.debug("screencast start on activate failed", exc_info=True)

    def set_bounds(self, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        # Kept for API compat. Viewport (not OS window) is what we size.
        if width >= 80 and height >= 80:
            return self.set_viewport("", int(width), int(height))
        return {"ok": False, "error": "tile too small"}

    def set_viewport(self, name: str, width: int, height: int, dpr: float = 1.0) -> dict[str, Any]:
        w = max(200, min(int(width or 0), 2400))
        h = max(160, min(int(height or 0), 1800))
        # Clicks are CSS pixels. Keep deviceScaleFactor at 1 so the screencast
        # bitmap and Input.dispatch* share the same space as the pane tile.
        # Sharpness comes from a 1:1 CSS viewport matching the tile, not from
        # a Retina override that then has to be remapped on every pointer.
        scale = 1.0
        self._viewport = {"width": w, "height": h, "dpr": scale}
        targets = [name] if name else list(self._tabs)
        for space in targets:
            try:
                sess = self._session(space)
                self._apply_metrics(sess)
                self._start_screencast(space, sess)
            except Exception:
                log.debug("set_viewport failed for %s", space, exc_info=True)
        return {"ok": True, **self._viewport}

    def _apply_metrics(self, sess: Any) -> None:
        w = int(self._viewport.get("width") or 1024)
        h = int(self._viewport.get("height") or 768)
        try:
            sess.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": w,
                    "height": h,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": w,
                    "screenHeight": h,
                    "dontSetVisibleSize": False,
                },
            )
        except Exception:
            log.debug("setDeviceMetricsOverride failed", exc_info=True)

    def _start_screencast(self, name: str, sess: Any) -> None:
        if not getattr(self, "_screencast", True):
            return
        w = int(self._viewport.get("width") or 1024)
        h = int(self._viewport.get("height") or 768)
        try:
            sess.call(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 85,
                    "maxWidth": w,
                    "maxHeight": h,
                    "everyNthFrame": 1,
                },
            )
        except Exception:
            log.debug("startScreencast failed", exc_info=True)
        # Drain any queued frames so latest_frame() is fresh.
        self._ingest_screencast(name, sess)

    def _enable_downloads(self, sess: Any) -> None:
        from . import downloads as dl

        dest = str(dl.ensure_downloads_dir())
        try:
            sess.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": dest, "eventsEnabled": True})
            return
        except Exception:
            log.debug("Browser.setDownloadBehavior failed", exc_info=True)
        try:
            sess.call("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": dest})
        except Exception:
            log.debug("Page.setDownloadBehavior failed", exc_info=True)

    def _ingest_downloads(self, name: str, events: list[dict[str, Any]]) -> None:
        from . import downloads as dl

        if not hasattr(self, "_dl_lock"):
            self._dl_lock = threading.Lock()
            self._dl_seen = set()
        for ev in events:
            method = ev.get("method") or ""
            params = ev.get("params") or {}
            if method == "Browser.downloadWillBegin":
                guid = str(params.get("guid") or "")
                if not guid:
                    continue
                with self._dl_lock:
                    self._dl_seen.add(guid)
                dl.record(
                    {
                        "id": guid,
                        "space": name,
                        "url": params.get("url") or "",
                        "filename": params.get("suggestedFilename") or "",
                        "state": "in_progress",
                    }
                )
            elif method in ("Browser.downloadProgress", "Page.downloadProgress"):
                guid = str(params.get("guid") or "")
                if not guid:
                    continue
                state = str(params.get("state") or "in_progress")
                path = params.get("filePath") or params.get("fileName") or ""
                rec = {
                    "id": guid,
                    "space": name,
                    "state": "completed" if state == "completed" else ("canceled" if state == "canceled" else "in_progress"),
                    "bytes": int(params.get("receivedBytes") or params.get("totalBytes") or 0),
                    "path": path,
                    "filename": Path(path).name if path else "",
                }
                dl.record(rec)

    def _ingest_screencast(self, name: str, sess: Any) -> None:
        import base64

        last_sid = None
        events = sess.drain_events(limit=80)
        try:
            self._ingest_downloads(name, events)
        except Exception:
            log.debug("download ingest failed", exc_info=True)
        for ev in events:
            if ev.get("method") != "Page.screencastFrame":
                continue
            params = ev.get("params") or {}
            data = params.get("data")
            sid = params.get("sessionId")
            if not data:
                continue
            try:
                raw = base64.b64decode(data)
            except Exception:
                continue
            meta = params.get("metadata") or {}
            with self._cast_lock:
                self._frames[name] = {
                    "bytes": raw,
                    "mime": "image/jpeg",
                    "width": int(meta.get("deviceWidth") or self._viewport.get("width") or 0),
                    "height": int(meta.get("deviceHeight") or self._viewport.get("height") or 0),
                    "ts": time.time(),
                }
            last_sid = sid
        if last_sid is not None:
            try:
                sess.call("Page.screencastFrameAck", {"sessionId": last_sid})
            except Exception:
                pass

    def latest_frame(self, name: str) -> dict[str, Any]:
        """Return the newest JPEG (or a still screenshot if screencast is quiet)."""
        try:
            sess = self._session(name)
            self._ingest_screencast(name, sess)
        except Exception:
            sess = None
        with self._cast_lock:
            rec = self._frames.get(name)
        if rec and rec.get("bytes") and (time.time() - float(rec.get("ts") or 0)) < 2.5:
            return rec
        # Fallback still so the pane is never blank after navigate.
        try:
            png = self.screenshot(name)
            return {
                "bytes": png,
                "mime": "image/png",
                "width": int(self._viewport.get("width") or 0),
                "height": int(self._viewport.get("height") or 0),
                "ts": time.time(),
            }
        except Exception as e:
            if rec and rec.get("bytes"):
                return rec
            return {"bytes": b"", "mime": "image/jpeg", "error": str(e)}

    def dispatch_input(self, name: str, event: dict[str, Any]) -> dict[str, Any]:
        ev = event or {}
        kind = str(ev.get("type") or "")
        if not kind:
            return {"ok": False, "error": "type required"}
        try:
            sess = self._session(name)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        params: dict[str, Any] = {"type": kind}
        if kind.startswith("mouse") or kind == "mouseWheel":
            # Clamp into the CSS viewport we last advertised so a stale
            # frontend coordinate cannot land off-page.
            vw = float(self._viewport.get("width") or 1024)
            vh = float(self._viewport.get("height") or 768)
            params["x"] = max(0.0, min(float(ev.get("x") or 0), vw))
            params["y"] = max(0.0, min(float(ev.get("y") or 0), vh))
            button = ev.get("button") or "left"
            if kind in ("mousePressed", "mouseReleased"):
                params["button"] = button
                params["clickCount"] = int(ev.get("clickCount") or 1)
                if kind == "mousePressed":
                    params["buttons"] = 1 if button == "left" else (2 if button == "right" else 4)
                else:
                    params["buttons"] = 0
            if kind == "mouseMoved":
                params["buttons"] = int(ev.get("buttons") or 0)
            if kind == "mouseWheel":
                params["deltaX"] = float(ev.get("deltaX") or 0)
                params["deltaY"] = float(ev.get("deltaY") or 0)
            if ev.get("modifiers"):
                params["modifiers"] = int(ev["modifiers"])
            try:
                sess.call("Input.dispatchMouseEvent", params)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "type": kind}
        if kind in ("keyDown", "keyUp", "char", "rawKeyDown"):
            key = str(ev.get("key") or "")
            text = str(ev.get("text") or "")
            params["key"] = key
            if text:
                params["text"] = text
            elif kind in ("keyDown", "char") and len(key) == 1:
                params["text"] = key
            if ev.get("modifiers"):
                params["modifiers"] = int(ev["modifiers"])
            if ev.get("windowsVirtualKeyCode"):
                params["windowsVirtualKeyCode"] = int(ev["windowsVirtualKeyCode"])
            try:
                sess.call("Input.dispatchKeyEvent", params)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "type": kind}
        return {"ok": False, "error": f"unsupported input type {kind!r}"}


_LAST_ENGINE_ERROR: str | None = None


def last_engine_error() -> str | None:
    return _LAST_ENGINE_ERROR


def _origin_of(url: str) -> str:
    from urllib.parse import urlparse

    p = urlparse(url or "")
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def _engine_pref() -> str:
    """settings.json ``browser_engine``: "cef" (default) | "chrome".

    Default is the native CEF tile. The chrome screencast engine is an
    explicit opt-in (Settings → 浏览器引擎) or ``GINNO_BROWSER_ENGINE=chrome``.
    """
    try:
        from .. import paths

        p = paths.settings_path()
        if p.exists():
            val = (json.loads(p.read_text() or "{}") or {}).get("browser_engine")
            if val in ("cef", "chrome"):
                return val
    except Exception:
        pass
    return "cef"


def choose_engine() -> BrowserEngine:
    """Pick the engine. Tests / CI force fake via ``GINNO_BROWSER_ENGINE=fake``.

    Default (pref=cef): CEF tile when the host is live; otherwise a
    placeholder FakeEngine until the supervisor promotes to CEF once the
    host finishes initializing — no silent Chrome fallback. pref=chrome
    (user opt-in) uses the screencast engine.
    """
    global _LAST_ENGINE_ERROR
    _LAST_ENGINE_ERROR = None
    forced = (os.environ.get("GINNO_BROWSER_ENGINE") or "").strip().lower()
    if forced in ("fake", "test") or os.environ.get("PYTEST_CURRENT_TEST"):
        return FakeEngine()
    pref = forced if forced in ("cef", "chrome", "chromium") else _engine_pref()
    if pref not in ("chrome", "chromium"):
        from .cef import try_cef

        eng = try_cef()
        if eng is not None:
            return eng
        _LAST_ENGINE_ERROR = (
            "CEF 宿主还没就绪；就绪后会自动切到内嵌浏览器"
            "（设置→浏览器引擎可改用 screencast）"
        )
        log.warning(_LAST_ENGINE_ERROR)
        return FakeEngine()
    binary = _find_chrome()
    if not binary:
        _LAST_ENGINE_ERROR = _LAST_ENGINE_ERROR or "本机没找到 Chrome / Chromium"
        return FakeEngine()
    try:
        return ChromeEngine(binary)
    except Exception as e:
        _LAST_ENGINE_ERROR = f"{type(e).__name__}: {e}"
        log.exception("chrome engine failed; falling back to fake")
    return FakeEngine()
