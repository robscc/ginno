"""Mods runtime binaries: settings, discovery, dev-mode broker spawn.

- settings: the ``mods.*`` block of settings.json (defaults merged at read,
  user keys preserved — connector read_config 同款), plus the write path the
  management API uses.
- discovery: node (mods.nodePath override → the external-agents
  ``_resolve_cli`` three-stage resolver) and the broker binary
  (mods.brokerPath → workspace target/{debug,release} → ~/.ginno/bin/).
- dev spawn: no Tauri env socket → spawn ``ginno-mod-broker --socket <path>``
  into $TMPDIR ourselves and supervise it (desktop injects
  GINNO_MOD_BROKER_SOCK and skips all of this).

Token convention: the broker creates the socket plus a sibling token file
``<socket>.token`` (0600, design §8.6); we read the token after spawn and
carry it in the hello handshake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("ginno.mods")

BROKER_BIN = "ginno-mod-broker"

DEFAULT_MODS_SETTINGS: dict = {
    "enabled": True,
    "allowOverrideDenyRules": False,
    "nodePath": "",   # '' = auto-discover (settings override, design §9)
    "brokerPath": "",  # '' = auto-discover (dev/web only)
    "contextWindow": 200_000,  # the runtime tracks no model window; $.session.usage().context.window proxies this
    "items": {},      # name → {enabled, source, grants, config}
}

MOD_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SOCK_NAME = "ginno-mod-broker.sock"
_TOKEN_SUFFIX = ".token"
_SPAWN_WAIT_S = 10.0

# dev-spawned broker child (module scope: survives across reconnects of the
# channel; the channel's stop() tears it down).
_broker_proc: asyncio.subprocess.Process | None = None
_broker_sock: str | None = None


# ---- settings ----------------------------------------------------------------


def mods_dir() -> Path:
    from .. import paths

    return paths.home() / "mods"


def load_mods_settings() -> dict:
    """The mods block with defaults merged in (user keys never clobbered)."""
    from .. import paths

    try:
        settings = json.loads(paths.settings_path().read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        settings = {}
    stored = settings.get("mods")
    cfg = dict(DEFAULT_MODS_SETTINGS)
    if isinstance(stored, dict):
        cfg.update(stored)
    if not isinstance(cfg.get("items"), dict):
        cfg["items"] = {}
    return cfg


def update_mods_settings(patch: dict) -> dict:
    """Merge ``patch`` into settings.json's mods block (read-modify-write of
    only that block — PUT /api/config owns the rest of the document)."""
    from .. import paths

    settings_path = paths.settings_path()
    try:
        settings = json.loads(settings_path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        settings = {}
    mods = settings.get("mods")
    if not isinstance(mods, dict):
        mods = dict(DEFAULT_MODS_SETTINGS)
    for key, value in patch.items():
        if value is None:
            continue
        mods[key] = value
    settings["mods"] = mods
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    return mods


def update_mod_item(name: str, patch: dict) -> dict:
    """Merge one mod's item (enabled/config/grants/source); creates it."""
    if not MOD_NAME_RE.match(name):
        raise ValueError(f"invalid mod name: {name!r}")
    cfg = load_mods_settings()
    items = dict(cfg.get("items") or {})
    item = dict(items.get(name) or {})
    for key, value in patch.items():
        if value is None:
            continue
        item[key] = value
    item.setdefault("enabled", True)
    item.setdefault("source", "global")
    items[name] = item
    update_mods_settings({"items": items})
    return item


# ---- binary discovery ----------------------------------------------------------


def _login_shell_path() -> str:
    """PATH extended with the usual GUI-blind-spot dirs (_resolve_cli 的
    显式补录集合); module-local copy so mods discovery never touches the
    external-agents CLI cache."""
    home = os.path.expanduser("~")
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/sbin"]
    for sub in (".local/bin", "bin"):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            extra.append(p)
    return os.pathsep.join([os.environ.get("PATH", ""), *extra])


def resolve_node() -> str | None:
    """Node ≥22.18 for the runners. mods.nodePath wins; else the three-stage
    PATH → extended-PATH → login-shell resolver (tools/external_agents/base.py
    的 _resolve_cli,GUI PATH 盲区修过的坑只踩一次)."""
    explicit = (load_mods_settings().get("nodePath") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    name = "node"
    found = shutil.which(name) or shutil.which(name, path=_login_shell_path())
    if not found:
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            r = subprocess.run(
                [shell, "-lc", f"command -v {name}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
                found = lines[0] if lines else None
        except Exception:  # noqa: BLE001 — discovery must never raise
            found = None
    return found or None


def _candidate_broker_paths() -> list[Path]:
    candidates: list[Path] = []
    explicit = (load_mods_settings().get("brokerPath") or "").strip()
    if explicit:
        candidates.append(Path(explicit))
        return candidates
    repo_root = Path(__file__).resolve().parents[5]  # …/ginno
    for profile in ("debug", "release"):
        candidates.append(repo_root / "target" / profile / BROKER_BIN)
    candidates.append(Path.home() / ".ginno" / "bin" / BROKER_BIN)
    return candidates


def resolve_broker() -> str | None:
    """The ginno-mod-broker CLI binary (dev/web mode). Desktop runs the same
    crate in-process and injects the socket via env instead."""
    for cand in _candidate_broker_paths():
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:
            continue
    return None


def resolve_runner() -> str | None:
    """The mod-runner.mjs single-file bundle the broker spawns per mod.
    Same discovery ladder as the broker binary: env override → repo dist →
    ~/.ginno/bin (where a packaged install materializes it)."""
    candidates: list[Path] = []
    env = os.environ.get("GINNO_MOD_RUNNER_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    repo_root = Path(__file__).resolve().parents[5]  # …/ginno
    candidates.append(repo_root / "packages" / "mod-runner" / "dist" / "mod-runner.mjs")
    candidates.append(Path.home() / ".ginno" / "bin" / "mod-runner.mjs")
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


# ---- dev-mode broker spawn + supervision -----------------------------------------


async def ensure_broker() -> tuple[str | None, str | None]:
    """Return (socket_path, token) for the dev-spawned broker, spawning one
    when none is running. The channel's connect loop calls this per attempt;
    spawn rate is bounded by its exponential backoff. Returns (None, None)
    when the binary or node is undiscoverable."""
    global _broker_proc, _broker_sock

    if _broker_proc is not None and _broker_proc.returncode is None and _broker_sock:
        token = _read_token(_broker_sock)
        if token:
            return _broker_sock, token
        # Token unreadable (broker mid-cleanup?): fall through and respawn.
    # A previous child died: reap it before respawning.
    if _broker_proc is not None and _broker_proc.returncode is not None:
        log.info("mods broker exited (rc=%s); respawning", _broker_proc.returncode)
        _broker_proc = None
        _broker_sock = None

    binary = resolve_broker()
    node = resolve_node()
    if not binary:
        log.info("mods broker binary not found — mods runtime unavailable")
        return None, None
    if not node:
        log.info("node not found — mods runtime unavailable (runner needs Node ≥22.18)")
        return None, None

    sock_path = os.path.join(tempfile.gettempdir(), _SOCK_NAME)
    _cleanup_stale_socket(sock_path)
    try:
        _broker_proc = await asyncio.create_subprocess_exec(
            binary,
            "--socket",
            sock_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GINNO_NODE_PATH": node},
        )
    except OSError as e:
        log.warning("mods broker spawn failed: %s: %s", type(e).__name__, e)
        _broker_proc = None
        return None, None
    _broker_sock = sock_path
    # Wait for the socket AND its token file: the broker creates the socket
    # first and the token a moment after — returning on the socket alone races
    # the token read and the hello then goes out with token=null.
    deadline = asyncio.get_running_loop().time() + _SPAWN_WAIT_S
    while asyncio.get_running_loop().time() < deadline:
        token = _read_token(sock_path)
        if token and os.path.exists(sock_path):
            if _broker_proc.returncode is not None:
                log.warning("mods broker died during startup (rc=%s)", _broker_proc.returncode)
                _broker_proc = None
                return None, None
            return sock_path, token
        await asyncio.sleep(0.05)
    log.warning("mods broker did not create %s within %.0fs", sock_path, _SPAWN_WAIT_S)
    await stop_broker()
    return None, None


def _read_token(sock_path: str) -> str | None:
    try:
        return (Path(sock_path + _TOKEN_SUFFIX).read_text() or "").strip() or None
    except OSError:
        return None


def _cleanup_stale_socket(sock_path: str) -> None:
    for path in (sock_path, sock_path + _TOKEN_SUFFIX):
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


async def stop_broker() -> None:
    """Terminate the dev-spawned broker (channel stop / app shutdown)."""
    global _broker_proc, _broker_sock
    proc, _broker_proc = _broker_proc, None
    _broker_sock = None
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        pass
    except Exception:  # noqa: BLE001 — shutdown must always complete
        log.exception("mods broker stop failed")


def scan_installed_mods() -> list[dict]:
    """Mod directories under ~/.ginno/mods with their manifest facts
    (``.claude-plugin/plugin.json``, the drop-in convention)."""
    out: list[dict] = []
    root = mods_dir()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir():
            continue
        name = d.name
        version = ""
        manifest = None
        for rel in (".claude-plugin/plugin.json", "plugin.json"):
            try:
                manifest = json.loads((d / rel).read_text() or "{}")
                break
            except (OSError, json.JSONDecodeError):
                continue
        if isinstance(manifest, dict):
            name = str(manifest.get("name") or name)
            version = str(manifest.get("version") or "")
        # Shape: the JS "modules" hooks.json rides the broker/runner; the
        # classic {"hooks": {...}} shape goes to the HookDispatcher instead
        # (§10) and must never be spawned as a runner (it has no module to
        # import — the runner would exit and crash-loop).
        shape = "none"
        for rel in ("hooks/hooks.json", "hooks.json"):
            try:
                doc = json.loads((d / rel).read_text() or "{}")
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(doc, dict):
                if isinstance(doc.get("modules"), list) and doc["modules"]:
                    shape = "js"
                elif isinstance(doc.get("hooks"), dict):
                    shape = "classic"
            break
        out.append({"name": name, "version": version, "path": str(d),
                    "hasManifest": manifest is not None, "shape": shape})
    return out


# ---- classic hooks bridging (claude-code-mods-design.md §10) ---------------------


def read_classic_hooks_doc(mod_dir: Path) -> dict | None:
    """A mod's hooks.json when it is the CLASSIC shape (``{"hooks": {...}}``);
    None for the JS "modules" shape (those hooks ride the broker/runner)."""
    doc = None
    for rel in ("hooks/hooks.json", "hooks.json"):
        try:
            doc = json.loads((mod_dir / rel).read_text() or "{}")
            break
        except (OSError, json.JSONDecodeError):
            continue
    if isinstance(doc, dict) and isinstance(doc.get("hooks"), dict):
        return doc
    return None


def register_classic_plugin_hooks(dispatcher: Any) -> int:
    """Scan ~/.ginno/mods/ and register every classic-shape hooks.json into
    the HookDispatcher (settings hooks stay first; env CLAUDE_PLUGIN_ROOT is
    injected at spawn). Runs at startup and after each install; never raises.
    Returns the number of hook commands registered."""
    if dispatcher is None:
        return 0
    count = 0
    for scanned in scan_installed_mods():
        doc = read_classic_hooks_doc(Path(scanned["path"]))
        if doc is None:
            continue
        try:
            count += dispatcher.register_plugin(scanned["name"], doc, scanned["path"])
        except Exception:  # noqa: BLE001 — one bad mod must not block the rest
            log.exception("classic hooks registration failed for %s", scanned["name"])
    if count:
        log.info("mods classic hooks registered (%d commands)", count)
    return count
