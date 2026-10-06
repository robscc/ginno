"""Mods management endpoints (claude-code-mods-design.md §5.1/§7.5).

- GET  /api/mods            — installed mods + channel/binary availability
- PUT  /api/mods/{name}     — enabled / config / grants (writes settings,
                              re-pushes config to the broker)
- POST /api/mods/install    — copy a local mod dir into ~/.ginno/mods/
- POST /api/mods/validate   — broker validate report (unsupported events /
                              unimplemented $ methods, §3.7)
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..server_shared import spawn_bg
from . import bridge_utils
from .channel import ChannelNotConnected, get_channel
from .ops import OpError

log = logging.getLogger("ginno.mods")

router = APIRouter()


class ModUpdateRequest(BaseModel):
    enabled: bool | None = None
    config: dict | None = Field(default=None)
    grants: dict | None = Field(default=None)
    source: str | None = None


class ModInstallRequest(BaseModel):
    path: str
    overwrite: bool = False


class ModValidateRequest(BaseModel):
    path: str | None = None
    name: str | None = None


def _merged_mod_list() -> list[dict]:
    """Installed dirs ∪ settings items ∪ broker-reported states, one row per
    mod (settings 页列表的数据源)."""
    channel = get_channel()
    settings = bridge_utils.load_mods_settings()
    items: dict = settings.get("items") or {}
    rows: dict[str, dict] = {}
    for scanned in bridge_utils.scan_installed_mods():
        rows[scanned["name"]] = {
            "name": scanned["name"],
            "version": scanned["version"],
            "installed": True,
            "hasManifest": scanned["hasManifest"],
        }
    for name, item in items.items():
        rows.setdefault(name, {"name": name, "version": "", "installed": False, "hasManifest": False})
        rows[name].update({k: v for k, v in item.items() if k != "name"})
    for name, state in channel.mods_state.items():
        rows.setdefault(name, {"name": name, "version": "", "installed": False, "hasManifest": False})
        state = dict(state)
        status = state.pop("status", None)
        if status:
            rows[name]["status"] = status
        rows[name].update({k: v for k, v in state.items() if k != "name"})
    for row in rows.values():
        row.setdefault("enabled", True)
        row.setdefault("status", None)
    return sorted(rows.values(), key=lambda r: r["name"])


@router.get("/api/mods")
async def list_mods() -> dict:
    channel = get_channel()
    channel.ensure_started()  # lazy connect: first settings-page visit pays
    return {
        "ok": True,
        "runtime": channel.availability(),
        "mods": _merged_mod_list(),
    }


@router.put("/api/mods/{name}")
async def update_mod(name: str, req: ModUpdateRequest) -> dict:
    if not bridge_utils.MOD_NAME_RE.match(name):
        return {"ok": False, "error": f"invalid mod name: {name!r}"}
    try:
        item = bridge_utils.update_mod_item(
            name,
            {
                "enabled": req.enabled,
                "config": req.config,
                "grants": req.grants,
                "source": req.source,
            },
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    # Config is ours; the broker diffs the re-push into per-mod reloads
    # (design §5.2). Fire-and-forget: the HTTP answer must not wait on IPC.
    ch = get_channel()
    if ch.is_connected:
        spawn_bg(_push_config_safe(ch))
    return {"ok": True, "mod": {"name": name, **item}}


async def _push_config_safe(channel) -> None:
    try:
        await channel.push_config()
    except Exception as e:  # noqa: BLE001 — the PUT already succeeded
        log.warning("mods config re-push failed: %s: %s", type(e).__name__, e)


@router.post("/api/mods/install")
async def install_mod(req: ModInstallRequest) -> dict:
    src = Path(req.path).expanduser()
    if not src.is_dir():
        return {"ok": False, "error": f"not a directory: {src}"}
    # Manifest → mod name (drop-in convention: .claude-plugin/plugin.json).
    name = ""
    version = ""
    for rel in (".claude-plugin/plugin.json", "plugin.json"):
        try:
            manifest = json.loads((src / rel).read_text() or "{}")
            name = str(manifest.get("name") or "")
            version = str(manifest.get("version") or "")
            if name:
                break
        except (OSError, json.JSONDecodeError):
            continue
    if not name:
        name = src.name
    if not bridge_utils.MOD_NAME_RE.match(name):
        return {"ok": False, "error": f"manifest name invalid: {name!r}"}
    dest = bridge_utils.mods_dir() / name
    if dest.exists():
        if not req.overwrite:
            return {"ok": False, "error": f"{name} is already installed", "exists": True}
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dest)
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    item = bridge_utils.update_mod_item(name, {"enabled": True, "source": "global"})
    get_channel().ensure_started()  # a first install is the trigger to connect
    # Classic-shape hooks.json (§10 bridging): register into the dispatcher
    # immediately — a settings-hook mod works before the broker is even up.
    try:
        from ..server_shared import _hooks

        bridge_utils.register_classic_plugin_hooks(_hooks)
    except Exception:  # noqa: BLE001 — install already succeeded
        log.exception("classic hooks re-registration failed after install")
    return {"ok": True, "mod": {"name": name, "version": version, **item}}


@router.post("/api/mods/validate")
async def validate_mod(req: ModValidateRequest) -> dict:
    """Broker validate (§3.7): hooks × events, $ calls × implementations.
    Forwards the broker's report verbatim; not connected → explicit error so
    the settings page can prompt to start the runtime first."""
    if req.path:
        target = str(Path(req.path).expanduser().resolve())
    elif req.name:
        target = str(bridge_utils.mods_dir() / req.name)
        if not Path(target).is_dir():
            return {"ok": False, "error": f"mod not installed: {req.name}"}
    else:
        return {"ok": False, "error": "path or name required"}
    channel = get_channel()
    channel.ensure_started()
    if not channel.is_connected:
        return {"ok": False, "error": "mods runtime is not connected", "runtime": channel.availability()}
    try:
        report = await channel.call(
            "broker",
            "validate",
            {"path": target, "nodePath": bridge_utils.resolve_node() or ""},
            timeout=30.0,
        )
    except OpError as e:
        return {"ok": False, "error": str(e), "code": e.code}
    except ChannelNotConnected:
        return {"ok": False, "error": "mods runtime is not connected"}
    except Exception as e:  # noqa: BLE001 — surface as payload, per house style
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "report": report, "id": uuid.uuid4().hex}
