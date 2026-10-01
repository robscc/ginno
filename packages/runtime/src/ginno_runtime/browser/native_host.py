"""Native messaging host installation (M3 port discovery, design §7.3).

The sidecar (a) writes its relay port to ~/.ginno/browser-relay-port for the
host script to read, (b) installs the host manifest into Chrome's
NativeMessagingHosts dir pointing at the bundled host script, with
``allowed_origins`` locked to our FIXED extension ID (the manifest ``key``
guarantees the ID — jlmheiiglpdikeihgjefjhmoakfllhkm).

Idempotent: safe to call at every startup; a user without the extension is
unaffected (Chrome simply never spawns the host).
"""

from __future__ import annotations

import logging
import shutil
import stat
from pathlib import Path

from .. import paths
from .config import extension_dir

log = logging.getLogger("ginno.browser.nativehost")

EXTENSION_ID = "jlmheiiglpdikeihgjefjhmoakfllhkm"
HOST_NAME = "app.ginno.connector"

_MAC_HOST_DIRS = [
    Path("~/Library/Application Support/Google/Chrome/NativeMessagingHosts"),
    Path("~/Library/Application Support/Chromium/NativeMessagingHosts"),
    Path("~/Library/Application Support/Microsoft Edge/NativeMessagingHosts"),
]
_LINUX_HOST_DIRS = [
    Path("~/.config/google-chrome/NativeMessagingHosts"),
    Path("~/.config/chromium/NativeMessagingHosts"),
]


def write_port_file(port: int) -> None:
    p = paths.home() / "browser-relay-port"
    try:
        p.write_text(str(port))
    except OSError:
        log.warning("cannot write relay port file %s", p)


def host_script_target() -> Path:
    """The host script location bundled inside the materialized extension
    folder (copied there by packages/extension/build.py)."""
    return extension_dir() / "ginno_browser_host.py"


def interpreter() -> str | None:
    import sys
    # Prefer the frozen runtime's own interpreter for the shebang; in dev,
    # sys.executable is the uv venv python.
    exe = Path(sys.executable)
    return str(exe) if exe.exists() else None


def install_host(force: bool = False) -> bool:
    """Install/refresh the native messaging manifests. Returns True if any
    manifest was written. Best-effort — never raises."""
    import platform

    script = host_script_target()
    if not script.exists():
        return False
    py = interpreter()
    if not py:
        return False
    # make the host executable with a shebang
    try:
        body = script.read_text()
        if not body.startswith("#!"):
            script.write_text(f"#!{py}\n{body}")
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        return False
    dirs = [d.expanduser() for d in
            (_MAC_HOST_DIRS if platform.system() == "Darwin" else _LINUX_HOST_DIRS)]
    manifest = {
        "name": HOST_NAME,
        "description": "Ginno Browser Connector port discovery",
        "path": str(script),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    import json

    wrote = False
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            m = d / f"{HOST_NAME}.json"
            if force or not m.exists() or m.read_text() != json.dumps(manifest, indent=2):
                m.write_text(json.dumps(manifest, indent=2))
                wrote = True
        except OSError:
            continue
    if wrote:
        log.info("native messaging host manifests installed (%s)", HOST_NAME)
    return wrote


def _bundled_version() -> str | None:
    """Version of the extension bundled with THIS build (None = unknown)."""
    import sys

    meipass = getattr(sys, "_MEIPASS", "")
    candidates = [
        Path(__file__).resolve().parents[4] / "extension" / "src",
        *([Path(meipass) / "extension_src"] if meipass else []),
    ]
    for d in candidates:
        mf = d / "manifest.json"
        if mf.exists():
            try:
                import json

                return json.loads(mf.read_text()).get("version")
            except (OSError, ValueError):
                return None
    return None


def materialize_extension() -> Path | None:
    """Copy the built extension source into ~/.ginno/browser-extension.

    Source order: materialized copy's sibling native-host script ships inside
    it already; when missing (first run), build from the packaged/repo
    extension source if present."""
    out = extension_dir()
    marker = out / "VERSION.json"
    src_version = _bundled_version()
    # 设计 §7.3 更新流:版本比对 → 重写文件 → Chrome 对 unpacked 目录热重载
    # → 扩展按既有重连逻辑回连。已物化且版本一致 → 不动(避免每次启动都触发
    # Chrome 的文件监听重载)。
    if marker.exists() and (out / "background.js").exists():
        try:
            import json as _json

            current = (_json.loads(marker.read_text()) or {}).get("version")
        except (OSError, ValueError):
            current = None
        if src_version is None or current == src_version:
            return out
        log.info("extension update %s → %s, re-materializing %s",
                 current, src_version, out)
    import sys

    meipass = getattr(sys, "_MEIPASS", "")
    candidates = [
        # dev checkout (…/packages/runtime/src/ginno_runtime/browser → packages/extension)
        Path(__file__).resolve().parents[4] / "extension",
        # frozen bundle (spec datas: extension_src/ + extension_src_native_host/)
        *([Path(meipass) / "extension_src"] if meipass else []),
    ]
    for src_root in candidates:
        frozen = (src_root / "manifest.json").exists()
        src_dir = src_root if frozen else src_root / "src"
        if not (src_dir / "manifest.json").exists():
            continue
        try:
            ns: dict = {}
            scripts_py = (src_root.parent / "runtime" / "src" /
                          "ginno_runtime" / "browser" / "scripts.py")
            out.mkdir(parents=True, exist_ok=True)
            (out / "content-scripts").mkdir(exist_ok=True)
            for name in ("background.js", "popup.html", "popup.js", "manifest.json"):
                shutil.copy2(src_dir / name, out / name)
            shutil.copy2(src_dir / "content-scripts" / "visual-indicator.js",
                         out / "content-scripts" / "visual-indicator.js")
            if scripts_py.exists():
                exec(compile(scripts_py.read_text(), str(scripts_py), "exec"), ns)
                (out / "content-scripts" / "accessibility-tree.js").write_text(
                    ns["ACCESSIBILITY_TREE_JS"])
                (out / "content-scripts" / "page-bridge.js").write_text(
                    ns["PAGE_BRIDGE_JS"])
            nh = src_root / "native-host" / "ginno_browser_host.py"
            if not nh.exists() and meipass:
                nh = Path(meipass) / "extension_src_native_host" / "ginno_browser_host.py"
            if nh.exists():
                shutil.copy2(nh, out / "ginno_browser_host.py")
            import json
            version = json.loads((src_dir / "manifest.json").read_text())["version"]
            (out / "VERSION.json").write_text(json.dumps(
                {"version": version, "extensionId": EXTENSION_ID}, indent=2))
            log.info("extension materialized → %s (v%s)", out, version)
            return out
        except OSError:
            log.exception("extension materialization failed")
            return None
    return None
