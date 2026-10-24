"""Mods management endpoints (claude-code-mods-design.md §5.1/§7.5).

- GET  /api/mods            — installed mods + channel/binary availability
- PUT  /api/mods/{name}     — enabled / config / grants (writes settings,
                              re-pushes config to the broker)
- POST /api/mods/install    — copy a local mod dir into ~/.ginno/mods/
- DELETE /api/mods/{name}   — uninstall: remove the install dir + settings
                              item, tear down registrations, re-push config
- POST /api/mods/validate   — broker validate report (unsupported events /
                              unimplemented $ methods, §3.7)
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ginno_runtime import jsonx as json

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
    mod (settings 页列表的数据源)。

    逻辑落在 ``bridge_utils.merged_mod_list``(连接器同步共用);这里保留
    模块级 ``get_channel`` 以便既有测试继续 monkeypatch 本模块的绑定。
    """
    return bridge_utils.merged_mod_list(get_channel())


def _sync_connectors() -> None:
    """mod 安装/卸载/启用/配置变更后,把动态连接器实例同步到注册表。
    连接器同步是尽力而为:失败绝不能影响 mods 主流程的返回。"""
    try:
        from ..connectors.mod_adapter import sync_mod_connectors

        sync_mod_connectors()
    except Exception:  # noqa: BLE001 — sync is additive
        log.exception("connector sync after mods mutation failed")


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
    _sync_connectors()
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
    _sync_connectors()
    return {"ok": True, "mod": {"name": name, "version": version, **item}}


@router.delete("/api/mods/{name}")
async def uninstall_mod(name: str) -> dict:
    """Uninstall: delete the dir that took effect (scan's scope rule —
    project copy wins; a same-name copy in the other scope survives and is
    NOT touched), drop the settings item, tear down every in-process
    registration, then re-push config so the broker's apply_config diff
    unloads the runner (per-process kill / host-mode worker)."""
    if not bridge_utils.MOD_NAME_RE.match(name):
        return {"ok": False, "error": f"invalid mod name: {name!r}"}
    channel = get_channel()
    row = next((r for r in bridge_utils.scan_installed_mods() if r["name"] == name), None)
    has_item = name in (bridge_utils.load_mods_settings().get("items") or {})
    if row is None and not has_item:
        # 无处可清(名字打错 / 早已卸干净):显式报错,不假装成功。
        return {"ok": False, "error": f"mod not installed: {name}"}
    removed = {"name": name, "path": "", "scope": ""}
    if row is not None:
        target = Path(row["path"])
        try:
            shutil.rmtree(target)
        except OSError as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        removed = {"name": name, "path": str(target), "scope": row.get("scope") or "global"}
    bridge_utils.remove_mod_item(name)
    # Classic-shape hooks entries (register_plugin only replaces — a plugin
    # that vanishes from disk needs the explicit unregister) plus the mod's
    # slice of the connection-scoped registries (commands/tools/agents): a
    # lingering entry would keep serving a mod that no longer exists.
    try:
        from ..server_shared import _hooks

        if _hooks is not None:
            _hooks.unregister_plugin(name)
    except Exception:  # noqa: BLE001 — uninstall must complete
        log.exception("classic hooks unregister failed after uninstall: %s", name)
    from ..agents import mod_registry
    from ..commands import mod_commands
    from ..tools import mod_tools

    mod_registry.unregister_mod(name)
    mod_commands.unregister_mod(name)
    mod_tools.unregister_mod(name)
    # Ghost row guard: the broker state lingers until the next hello; without
    # this the merged list renders the uninstalled mod as a "not installed" row.
    channel.mods_state.pop(name, None)
    # Fire-and-forget, same as PUT: the HTTP answer must not wait on IPC.
    if channel.is_connected:
        spawn_bg(_push_config_safe(channel))
    _sync_connectors()
    return {"ok": True, "removed": removed}


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


# ---- marketplace (mod-marketplace-design.md §6.2) -------------------------------


@router.get("/api/mods/marketplace")
async def marketplace_list(refresh: bool = False) -> dict:
    """All sources with catalogs (cache-first; ``refresh=1`` forces a fetch).
    Offline → stale cache + ``error`` reason per source, never a 500."""
    from . import marketplace as mkt

    try:
        sources = await mkt.list_marketplace(refresh=refresh)
    except Exception as e:  # noqa: BLE001 — surfacing beats a 500
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "sources": []}
    installed = {name: (item.get("origin") or None)
                 for name, item in bridge_utils.load_mods_settings().get("items", {}).items()}
    return {"ok": True, "sources": sources, "installedOrigins": installed}


@router.get("/api/mods/marketplace/item")
async def marketplace_get_item(src: str, path: str) -> dict:
    """One plugin's manifest + README + hook modules (detail view input)."""
    from . import marketplace as mkt

    return await mkt.marketplace_item(src, path)


class MarketInstallRequest(BaseModel):
    src: str
    path: str
    overwrite: bool = False


@router.post("/api/mods/marketplace/install")
async def marketplace_install(req: MarketInstallRequest) -> dict:
    from . import marketplace as mkt

    result = await mkt.install_from_market(req.src, req.path, overwrite=req.overwrite)
    _sync_connectors()
    return result


@router.post("/api/mods/marketplace/update")
async def marketplace_update(name: str) -> dict:
    from . import marketplace as mkt

    result = await mkt.update_from_market(name)
    _sync_connectors()
    return result


class MarketSourcesRequest(BaseModel):
    sources: list[dict]


@router.put("/api/mods/marketplace/sources")
async def marketplace_put_sources(req: MarketSourcesRequest) -> dict:
    from . import marketplace as mkt

    saved = mkt.save_sources(req.sources or [])
    return {"ok": True, "sources": saved}


@router.post("/api/mods/prompt-read")
async def mods_prompt_read(data: dict) -> dict:
    """Frontend answers ``mod.prompt.read`` ($.prompt.read op): ships the
    composer draft for the session. Best-effort — no waiter = no-op."""
    from . import ops as _ops

    ok = _ops.resolve_prompt_read(str(data.get("session_id") or ""), str(data.get("text") or ""))
    return {"ok": ok}
