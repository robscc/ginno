"""CEF runtime probe (M2).

A live ``CefEngine`` needs all three:

1. ``Chromium Embedded Framework.framework`` next to the .app
2. ``Ginno Helper.app`` (and GPU / Plugin / Renderer variants)
3. The Tauri host actually ``cef_initialize``'d and wrote a live CDP
   port to ``~/.ginno/browser/cef-cdp.json``

Helpers on disk without a ready host are **not** a native tile —
``try_cef()`` stays ``None`` and Chrome screencast remains the paint
path. Space / ownership stay in the sidecar. Rust only hosts the NSView
and the CEF child (``ginno:browser-tile`` geometry).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import spaces as space_store


_FW_NAME = "Chromium Embedded Framework.framework"


def cef_runtime_dir() -> Path | None:
    env = (os.environ.get("GINNO_CEF_DIR") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p
    # Packaged .app: Contents/Frameworks/Chromium Embedded Framework.framework
    me = Path(__file__).resolve()
    for parent in [me, *me.parents]:
        fw = parent / "Frameworks" / _FW_NAME
        if fw.is_dir():
            return fw.parent
        fw = parent / "Contents" / "Frameworks" / _FW_NAME
        if fw.is_dir():
            return fw.parent
    return None


def cef_helpers_present(root: Path | None = None) -> bool:
    """True when at least one CEF Helper.app sits next to the framework."""
    base = root or cef_runtime_dir()
    if base is None:
        return False
    # Typical layout: Contents/Frameworks/{Chromium Embedded Framework.framework,
    # Ginno Helper.app, Ginno Helper (GPU).app, …}
    for child in base.iterdir() if base.is_dir() else []:
        name = child.name
        if child.is_dir() and name.endswith(".app") and "Helper" in name:
            return True
    return False


def cef_status_path() -> Path:
    return space_store.browser_dir() / "cef-cdp.json"


def read_cef_status() -> dict[str, Any] | None:
    p = cef_status_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def cef_host_port(timeout: float = 0.6) -> int | None:
    """Return the host's CDP port only when the file is ready *and* CDP answers.

    A leftover file from a previous launch, or helpers-without-host, is
    not a live tile. No status file → None without spinning the budget.
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        rec = read_cef_status()
        if rec is None:
            return None
        if not rec.get("ready"):
            return None
        try:
            port = int(rec.get("port") or 0)
        except (TypeError, ValueError):
            return None
        if not (1024 <= port <= 65535):
            return None
        if _cdp_up(port):
            return port
        if time.time() >= deadline:
            return None
        time.sleep(0.05)


def _cdp_up(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=0.4) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def try_cef() -> Any | None:
    """Return a CefEngine only when helpers exist *and* the host CDP is live."""
    root = cef_runtime_dir()
    if root is None:
        return None
    if not cef_helpers_present(root):
        return None
    port = cef_host_port(timeout=1.2)
    if port is None:
        return None
    try:
        return CefEngine(port)
    except Exception:
        return None


class CefEngine:
    """Native CEF tile. Thin wrapper around ChromeEngine pointed at the host."""

    kind = "cef"

    def __init__(self, port: int | None = None) -> None:
        root = cef_runtime_dir()
        if root is None or not cef_helpers_present(root):
            raise RuntimeError(
                "CEF helpers are not packaged in this build. "
                "Chrome screencast (engine=chrome) is the live paint path."
            )
        live = int(port) if port else cef_host_port(timeout=1.5)
        if not live:
            raise RuntimeError(
                "CEF host process is not live. "
                "Chrome screencast (engine=chrome) is the live paint path."
            )
        from .engine import ChromeEngine

        self._inner = ChromeEngine(attach_port=live, screencast=False)

    def headed(self) -> bool:
        return True

    def _activate(self, name: str):
        tid = self._inner._activate(name)
        try:
            # 151 dock: tell the C host which lifted browser window to show.
            # Slot = the target's index in creation order (_target_order), which
            # matches the C host's g_docked adoption order; fall back to the
            # /json/list index if the target isn't tracked yet.
            order = getattr(self._inner, "_target_order", None) or []
            if tid in order:
                slot = order.index(tid)
            else:
                ids = [t.get("id") for t in self._inner._page_targets()]
                slot = ids.index(tid) if tid in ids else 0
            self._write_show(slot)
        except Exception:
            pass
        return tid

    @staticmethod
    def _rpc(cmd: dict) -> bool:
        """Synchronous RPC to the in-process C host over a Unix socket."""
        import socket as _socket

        base = os.environ.get("GINNO_HOME") or str(Path.home() / ".ginno")
        sp = Path(base) / "browser" / "cef-rpc.sock"
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(str(sp))
            s.sendall(json.dumps(cmd).encode())
            s.recv(64)  # ack
            s.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _write_cmd(cmd: dict) -> None:
        if CefEngine._rpc(cmd):
            return
        # Fallback to the old file channel if the socket isn't up.
        base = os.environ.get("GINNO_HOME") or str(Path.home() / ".ginno")
        p = Path(base) / "browser" / "cef-cmd.json"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cmd))
        except Exception:
            pass

    def _write_show(self, slot: int) -> None:
        self._write_cmd({"op": "show", "slot": slot})

    def show(self, name: str) -> None:
        """Show this session's browser window, hide the others (single visible)."""
        self._activate(name)

    def show_only(self, name: str) -> None:
        """Show this session's window if it already exists; never create/navigate."""
        tid = self._inner._tabs.get(name)
        if not tid:
            return
        order = getattr(self._inner, "_target_order", None) or []
        slot = order.index(tid) if tid in order else 0
        self._write_show(slot)

    def hide_all(self) -> None:
        """Hide every docked browser window (non-workspace route)."""
        self._write_cmd({"op": "hide_all"})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
