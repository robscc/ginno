"""Mod marketplace backend (mod-marketplace-design.md §3/§5/§6).

A marketplace is a git repository with a ``.claude-plugin/marketplace.json``
catalog at its root — the Claude Code official schema subset. Repos are
shallow-cloned into ``~/.ginno/cache/mods-market/<slug>`` and refreshed on a
TTL; every read path is offline-tolerant (a failed fetch serves the stale
cache with a reason string, never raises).

Trust rules live in the install path: preview scoring (validate + op table)
is separate; this module only discovers, fetches, and copies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from .. import paths
from . import bridge_utils

log = logging.getLogger("ginno.mods.marketplace")

# Market cache TTL (design §4.3): an open-drawer fetch at most every 5 min.
_REFRESH_TTL_S = 300.0

# Default sources (design §3.1): the curated Ginno source ships enabled; the
# full community dump stays opt-in (most of it scores 🔴 on Ginno today).
DEFAULT_SOURCES: list[dict] = [
    {"name": "ginno-mods", "repo": "https://github.com/robscc/ginno-mods", "enabled": True},
    {
        "name": "claude-plugins-community",
        "repo": "https://github.com/anthropics/claude-plugins-community",
        "enabled": False,
    },
]

_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,64}$")


def _market_cache_dir() -> Path:
    d = paths.home() / "cache" / "mods-market"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_sources() -> list[dict]:
    """settings.json ``mods.marketplaces`` (order preserved), seeded with the
    defaults on first read."""
    settings = bridge_utils.load_mods_settings()
    srcs = settings.get("marketplaces")
    if not isinstance(srcs, list) or not srcs:
        return [dict(s) for s in DEFAULT_SOURCES]
    return [dict(s) for s in srcs if isinstance(s, dict) and s.get("repo")]


def save_sources(srcs: list[dict]) -> list[dict]:
    cleaned = [
        {"name": str(s.get("name") or "").strip() or _slug_of(str(s.get("repo") or "")),
         "repo": str(s.get("repo") or "").strip(),
         "enabled": bool(s.get("enabled"))}
        for s in srcs
        if isinstance(s, dict) and str(s.get("repo") or "").strip()
    ]
    bridge_utils.update_mods_settings({"marketplaces": cleaned})
    return cleaned


def _slug_of(repo: str) -> str:
    tail = repo.rstrip("/").rsplit("/", 1)[-1] or "market"
    return tail.removesuffix(".git") or "market"


async def _git(args: list[str], cwd: Path | None = None, timeout: float = 60.0) -> str:
    """Run git; raises RuntimeError with stderr on failure (callers translate)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("git timed out") from None
    if proc.returncode != 0:
        raise RuntimeError((err or b"").decode(errors="replace").strip()[:300])
    return (out or b"").decode(errors="replace")


async def _ensure_repo(source: dict, *, refresh: bool) -> tuple[Path, dict]:
    """Clone-or-fetch one source. Returns (repo_dir, meta) where meta carries
    ``commit``/``fetched_at``/``error`` — the catalog read is best-effort on
    top of whatever the cache holds."""
    repo = str(source.get("repo") or "")
    slug = str(source.get("name") or _slug_of(repo))
    d = _market_cache_dir() / slug
    meta_path = d.parent / f"{slug}.meta.json"
    meta: dict = {}
    try:
        meta = json.loads(meta_path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        pass

    fresh = bool(meta.get("fetched_at")) and time.time() - float(meta.get("fetched_at")) < _REFRESH_TTL_S
    if fresh and not refresh:
        return d, meta

    try:
        if not (d / ".git").exists():
            d.mkdir(parents=True, exist_ok=True)
            await _git(["clone", "--depth", "1", repo, str(d)], timeout=120.0)
        else:
            await _git(["fetch", "--depth", "1", "origin"], cwd=d)
            await _git(["reset", "--hard", "FETCH_HEAD"], cwd=d)
        commit = (await _git(["rev-parse", "--short", "HEAD"], cwd=d)).strip()
        meta = {"commit": commit, "fetched_at": time.time(), "error": ""}
    except (RuntimeError, OSError, FileNotFoundError) as e:
        # git missing or offline: keep serving the stale cache (if any).
        meta["error"] = f"{type(e).__name__}: {e}"[:300]
        meta.setdefault("fetched_at", 0.0)
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
    except OSError:
        pass
    return d, meta


def _read_catalog(repo_dir: Path) -> dict:
    """marketplace.json → {name, plugins:[...]}; tolerant of absence (a repo
    whose root IS a plugin is treated as a single-entry catalog)."""
    cat_path = repo_dir / ".claude-plugin" / "marketplace.json"
    try:
        cat = json.loads(cat_path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        cat = {}
    plugins = cat.get("plugins")
    if not isinstance(plugins, list):
        # A single-plugin repo: the root carries plugin.json.
        plugins = [{"name": repo_dir.name, "source": "."}] if (repo_dir / ".claude-plugin" / "plugin.json").exists() else []
    out = []
    for p in plugins:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        src = str(p.get("source") or "").strip()
        if not name or not src or not _NAME_RE.match(name):
            continue
        out.append({
            "name": name,
            "source": src,
            "description": str(p.get("description") or ""),
            "version": str(p.get("version") or ""),
            "tags": [str(t) for t in (p.get("tags") or []) if isinstance(t, str)],
        })
    return {"name": str(cat.get("name") or repo_dir.name), "plugins": out}


def _plugin_manifest(repo_dir: Path, source: str) -> dict:
    """One plugin's manifest + README (design §4.1 detail view)."""
    pdir = (repo_dir / source).resolve()
    try:
        pdir.relative_to(repo_dir.resolve())
    except ValueError:
        return {"ok": False, "error": "source escapes the marketplace repo"}
    manifest: dict = {}
    for rel in (".claude-plugin/plugin.json", "plugin.json"):
        try:
            manifest = json.loads((pdir / rel).read_text() or "{}")
            if manifest.get("name"):
                break
        except (OSError, json.JSONDecodeError):
            continue
    readme = ""
    for rel in ("README.md", "readme.md"):
        try:
            readme = (pdir / rel).read_text(errors="replace")
            if readme:
                break
        except OSError:
            continue
    hooks: list = []
    for rel in ("hooks/hooks.json", "hooks.json"):
        try:
            data = json.loads((pdir / rel).read_text() or "{}")
            if isinstance(data, dict) and "modules" in data:
                hooks = data["modules"]
                break
        except (OSError, json.JSONDecodeError):
            continue
    return {
        "ok": True,
        "path": str(pdir),
        "manifest": manifest,
        "readme": readme[:20_000],
        "hookModules": hooks,
    }


async def list_marketplace(refresh: bool = False) -> list[dict]:
    """All enabled sources with their catalogs (cached; ``refresh`` forces)."""
    out = []
    for source in load_sources():
        if not source.get("enabled"):
            out.append({**source, "plugins": [], "skipped": True})
            continue
        d, meta = await _ensure_repo(source, refresh=refresh)
        cat = _read_catalog(d) if (d / ".git").exists() else {"name": source.get("name"), "plugins": []}
        out.append({
            **source,
            "commit": meta.get("commit") or "",
            "fetched_at": meta.get("fetched_at") or 0.0,
            "error": meta.get("error") or "",
            "plugins": cat["plugins"],
        })
    return out


async def marketplace_item(src_name: str, plugin: str) -> dict:
    for source in load_sources():
        if source.get("name") != src_name and source.get("repo") != src_name:
            continue
        d, _meta = await _ensure_repo(source, refresh=False)
        cat = _read_catalog(d)
        entry = next((p for p in cat["plugins"] if p["name"] == plugin), None)
        if entry is None:
            return {"ok": False, "error": f"plugin {plugin!r} not in {src_name!r}"}
        detail = _plugin_manifest(d, entry["source"])
        detail["entry"] = entry
        detail["source"] = source
        detail["commit"] = _meta.get("commit") or ""
        return detail
    return {"ok": False, "error": f"unknown marketplace source: {src_name!r}"}


def installed_origin(name: str) -> dict | None:
    item = bridge_utils.load_mods_settings().get("items", {}).get(name) or {}
    origin = item.get("origin")
    return dict(origin) if isinstance(origin, dict) else None


async def install_from_market(src_name: str, plugin: str, *, overwrite: bool = False) -> dict:
    """design §4.2: fetch → copy into ~/.ginno/mods/<name> → enable + origin
    metadata. Reuses the local-install semantics (manifest validation, classic
    hook re-registration) by delegating to api.install_mod's directory copy."""
    detail = await marketplace_item(src_name, plugin)
    if not detail.get("ok"):
        return detail
    from . import api as mods_api

    req = mods_api.ModInstallRequest(path=detail["path"], overwrite=overwrite)
    result = await mods_api.install_mod(req)
    if result.get("ok") and result.get("mod", {}).get("name"):
        name = result["mod"]["name"]
        source = detail["source"]
        entry = detail["entry"]
        bridge_utils.update_mod_item(name, {
            "origin": {
                "marketplace": source.get("name") or _slug_of(str(source.get("repo") or "")),
                "repo": source.get("repo") or "",
                "path": entry["source"],
                "commit": detail.get("commit") or "",
                "version": entry.get("version") or str((detail.get("manifest") or {}).get("version") or ""),
                "installed_at": time.time(),
            }
        })
        result["mod"] = {**result["mod"], "origin": installed_origin(name)}
    return result


async def update_from_market(name: str) -> dict:
    """design §4.3: refresh the source, re-install, keep user config/grants.
    The diff surface (version/hooks/classic commands) is computed by the
    caller from before/after manifests — this is the mechanical update."""
    origin = installed_origin(name)
    if not origin:
        return {"ok": False, "error": f"{name!r} was not installed from a marketplace"}
    detail = await marketplace_item(origin.get("marketplace") or "", name)
    if not detail.get("ok"):
        return detail
    from . import api as mods_api

    req = mods_api.ModInstallRequest(path=detail["path"], overwrite=True)
    result = await mods_api.install_mod(req)
    if result.get("ok"):
        bridge_utils.update_mod_item(name, {
            "origin": {**origin,
                       "commit": detail.get("commit") or origin.get("commit") or "",
                       "version": str((detail.get("manifest") or {}).get("version") or origin.get("version") or ""),
                       "installed_at": time.time()},
        })
        result["mod"] = {**result.get("mod", {}), "origin": installed_origin(name)}
    return result
