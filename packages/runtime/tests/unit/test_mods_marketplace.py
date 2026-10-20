"""Marketplace backend tests (mod-marketplace-design.md §3/§6). Offline: git
is faked with a plain directory copy — the repo cache shape (.git marker) is
manufactured directly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime.mods import bridge_utils, marketplace as mkt

pytestmark = pytest.mark.unit


def _make_repo(base: Path, name: str, plugins: list[dict]) -> Path:
    repo = base / name
    (repo / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)  # marker only — no real git in tests
    for p in plugins:
        pdir = repo / p["source"]
        (pdir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (pdir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": p["name"], "version": p.get("version", "0.1.0")})
        )
        (pdir / "README.md").write_text(f"# {p['name']}\nhello")
        if p.get("js"):
            (pdir / "hooks").mkdir(exist_ok=True)
            (pdir / "hooks" / "hooks.json").write_text(json.dumps({"modules": ["./m.mjs"]}))
    (repo / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": name, "plugins": [
            {"name": p["name"], "source": p["source"], "description": p.get("description", ""),
             "version": p.get("version", "")} for p in plugins
        ]})
    )
    return repo


@pytest.fixture(autouse=True)
def no_channel(monkeypatch):
    """install_mod 触发真实 broker 连接(ensure_started)——测试里换成 no-op,
    classic hooks 注册同样跳过(_hooks 未初始化会走异常分支,无碍)。"""
    class _Ch:
        def ensure_started(self):
            pass

        @property
        def is_connected(self):
            return False

    from ginno_runtime.mods import api as mods_api

    monkeypatch.setattr(mods_api, "get_channel", lambda: _Ch())


@pytest.fixture
def fake_market(monkeypatch, isolated_home):
    """One source whose repo cache pre-exists (git never runs)."""
    base = isolated_home / "repos"
    _make_repo(base, "ginno-mods", [
        {"name": "alpha", "source": "./alpha", "description": "A", "js": True},
        {"name": "beta", "source": "./beta"},
    ])
    cache = isolated_home / "cache" / "mods-market"
    cache.mkdir(parents=True)
    import shutil

    shutil.copytree(base / "ginno-mods", cache / "ginno-mods")
    (cache / "ginno-mods.meta.json").write_text(json.dumps({"commit": "abc1234", "fetched_at": 1e18, "error": ""}))

    async def fake_ensure(source, *, refresh):
        return cache / str(source.get("name")), {"commit": "abc1234", "fetched_at": 1e18, "error": ""}

    monkeypatch.setattr(mkt, "_ensure_repo", fake_ensure)
    monkeypatch.setattr(
        mkt, "load_sources",
        lambda: [{"name": "ginno-mods", "repo": "https://x/gg", "enabled": True}],
    )
    return cache


def test_list_catalog_and_installed_origins(fake_market):
    import asyncio

    rows = asyncio.run(mkt.list_marketplace())
    assert rows[0]["name"] == "ginno-mods" and rows[0]["commit"] == "abc1234"
    assert [p["name"] for p in rows[0]["plugins"]] == ["alpha", "beta"]


def test_item_detail(fake_market):
    import asyncio

    detail = asyncio.run(mkt.marketplace_item("ginno-mods", "alpha"))
    assert detail["ok"] and detail["manifest"]["name"] == "alpha"
    assert detail["readme"].startswith("# alpha")
    assert detail["hookModules"] == ["./m.mjs"]


def test_install_writes_origin_metadata(fake_market):
    import asyncio

    result = asyncio.run(mkt.install_from_market("ginno-mods", "alpha"))
    assert result["ok"], result
    name = result["mod"]["name"]
    assert name == "alpha"
    assert (bridge_utils.mods_dir() / "alpha" / ".claude-plugin" / "plugin.json").exists()
    origin = result["mod"]["origin"]
    assert origin["marketplace"] == "ginno-mods"
    assert origin["commit"] == "abc1234" and origin["version"] == "0.1.0"
    # settings item carries origin + enabled
    item = bridge_utils.load_mods_settings()["items"]["alpha"]
    assert item["enabled"] is True and item["origin"]["path"] == "./alpha"


def test_update_reinstalls_and_bumps_meta(fake_market):
    import asyncio

    asyncio.run(mkt.install_from_market("ginno-mods", "alpha"))
    result = asyncio.run(mkt.update_from_market("alpha"))
    assert result["ok"]
    assert result["mod"]["origin"]["version"] == "0.1.0"


def test_sources_roundtrip(monkeypatch, isolated_home):
    saved = mkt.save_sources([
        {"repo": "https://github.com/foo/bar", "enabled": True},
        {"bad": True},  # dropped — no repo
    ])
    assert saved == [{"name": "bar", "repo": "https://github.com/foo/bar", "enabled": True}]
    assert bridge_utils.load_mods_settings()["marketplaces"] == saved


def test_catalog_tolerates_single_plugin_repo(isolated_home):
    repo = isolated_home / "solo"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "solo"}))
    cat = mkt._read_catalog(repo)
    assert cat["plugins"] == [{"name": "solo", "source": ".", "description": "", "version": "", "tags": []}]
