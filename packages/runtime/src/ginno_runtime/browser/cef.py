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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
