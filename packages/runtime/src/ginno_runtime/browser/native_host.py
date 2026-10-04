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
import sys
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
    manifest was written. Best-effort — never raises.

    Entry the manifest points at:
    - dev: the materialized shim with a python shebang (sidecar venv has
      ginno_runtime importable)
    - frozen: a tiny .sh wrapper exec-ing the bundled binary with
      ``--native-host`` (the binary's sys.executable IS the app — a python
      shebang would be wrong there)
    """
    import json
    import platform

    script = host_script_target()
    if not script.exists():
        return False
    frozen = bool(getattr(sys, "frozen", False))
    try:
        if frozen:
            target = script.parent / "ginno_browser_host.sh"
            target.write_text(
                f"#!/bin/sh\nexec \"{sys.executable}\" --native-host\n")
            target.chmod(target.stat().st_mode
                         | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            py = interpreter()
            if not py:
                return False
            target = script
            body = script.read_text()
            if not body.startswith("#!"):
                script.write_text(f"#!{py}\n{body}")
                script.chmod(script.stat().st_mode
                             | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        return False
    dirs = [d.expanduser() for d in
            (_MAC_HOST_DIRS if platform.system() == "Darwin" else _LINUX_HOST_DIRS)]
    manifest = {
        "name": HOST_NAME,
        "description": "Ginno Browser Connector port discovery",
        "path": str(target),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
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
        log.info("native messaging host manifests installed (%s → %s)",
                 HOST_NAME, target)
    return wrote


def _materialize_candidates() -> list[Path]:
    """Materialization roots (dev 带 src/ 子层,frozen 平铺)."""
    meipass = getattr(sys, "_MEIPASS", "")
    return [
        # dev checkout (…/packages/runtime/src/ginno_runtime/browser → packages/extension)
        Path(__file__).resolve().parents[4] / "extension",
        # frozen bundle (spec datas: extension_src/ + extension_src_native_host/)
        *([Path(meipass) / "extension_src"] if meipass else []),
    ]


def _source_candidates() -> list[Path]:
    """Extension source roots, dev checkout first, frozen bundle second."""
    meipass = getattr(sys, "_MEIPASS", "")
    return [
        Path(__file__).resolve().parents[4] / "extension" / "src",
        *([Path(meipass) / "extension_src"] if meipass else []),
    ]


def _ext_messages_candidates() -> list[Path]:
    """Roots holding {en,zh-CN}/ext.json for _locales generation (§8).

    dev: repo checkout (…/ginno/apps/web/messages，ginno_runtime/browser 的
    parents[5] = 仓库根)。frozen: Makefile --add-data 把 apps/web/messages
    整树放进 bundle 的 web_messages/（目录结构与 dev 完全一致，共用
    ext_locales.load_catalogs）。"""
    meipass = getattr(sys, "_MEIPASS", "")
    return [
        Path(__file__).resolve().parents[5] / "apps" / "web" / "messages",
        *([Path(meipass) / "web_messages"] if meipass else []),
    ]


def _bundled_version() -> str | None:
    """Version of the extension bundled with THIS build (None = unknown)."""
    candidates = _source_candidates()
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

    candidates = _materialize_candidates()
    for src_root in candidates:
        frozen = (src_root / "manifest.json").exists()
        src_dir = src_root if frozen else src_root / "src"
        if not (src_dir / "manifest.json").exists():
            continue
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "content-scripts").mkdir(exist_ok=True)
            for name in ("background.js", "popup.html", "popup.js", "manifest.json"):
                if (src_dir / name).exists():
                    shutil.copy2(src_dir / name, out / name)
            if (src_dir / "icons").is_dir():
                shutil.copytree(src_dir / "icons", out / "icons",
                                dirs_exist_ok=True)
            shutil.copy2(src_dir / "content-scripts" / "visual-indicator.js",
                         out / "content-scripts" / "visual-indicator.js")
            # content scripts 单一来源(scripts.py):dev/frozen 一律直接 import。
            # 旧实现按仓库相对路径找 scripts.py,frozen bundle 里该路径不存在,
            # 两个生成脚本被静默跳过 → Chrome 加载扩展报缺文件(2026-10-02)。
            try:
                from . import scripts as _scripts

                (out / "content-scripts" / "accessibility-tree.js").write_text(
                    _scripts.ACCESSIBILITY_TREE_JS)
                (out / "content-scripts" / "page-bridge.js").write_text(
                    _scripts.PAGE_BRIDGE_JS)
            except Exception:  # noqa: BLE001 — 生成失败不中断整个物化
                log.exception("content-script generation failed")
            # i18n(§8):_locales 从 web ext.json 生成 + manifest 改写 __MSG_*，
            # 与 packages/extension/build.py 同一单一来源（browser/ext_locales）。
            # 生成失败不中断物化：manifest 保持英文明文，扩展侧内嵌 en 兜底
            # （与 content-script 生成失败同策略）；generate_and_localize 内部
            # 保证 manifest 只在 default locale messages 落盘后才改写。
            try:
                from . import ext_locales as _ext_locales

                catalogs = None
                for root in _ext_messages_candidates():
                    catalogs = _ext_locales.load_catalogs(root)
                    if catalogs is not None:
                        break
                if catalogs is not None:
                    _ext_locales.generate_and_localize(out, catalogs)
                else:
                    log.warning("ext catalogs not found; _locales skipped")
            except Exception:  # noqa: BLE001 — 生成失败不中断整个物化
                log.exception("extension _locales generation failed")
            nh = src_root / "native-host" / "ginno_browser_host.py"
            if not nh.exists():
                nh = (Path(getattr(sys, "_MEIPASS", "")) / "extension_src_native_host"
                      / "ginno_browser_host.py")
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


def run_host_entry() -> None:
    """``ginno-runtime --native-host``: run the host protocol IN-PROCESS.

    The frozen binary's sys.executable is the app itself — spawning it with a
    script arg would boot the server, so we import the protocol module
    directly (same code the dev-mode standalone shim imports).
    """
    from .host_protocol import main as _host_main

    _host_main()
