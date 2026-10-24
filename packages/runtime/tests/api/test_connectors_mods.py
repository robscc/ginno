"""S1/S2 backend: Mods as dynamic connectors (mods-connector-migration-design
§3) + the unified config descriptor.

Real FastAPI stack, an isolated home, and a bare ModChannel singleton (never
connected) — no broker, no node, no network.
"""

from __future__ import annotations

import importlib

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.mods import bridge_utils
from ginno_runtime.mods import channel as mods_channel

pytestmark = pytest.mark.api

DEFAULTS = {
    "units": "metric",
    "retries": 3,
    "ratio": 0.5,
    "verbose": True,
    "tags": ["alpha"],
    "advanced": {"nested": 1},  # dict → 仅高级 JSON,不进 properties
}


@pytest.fixture(autouse=True)
def fresh_connector_state(monkeypatch):
    """每个测试一份注册表 + channel 单例,避免进程级状态串味。"""
    reg_mod = importlib.import_module("ginno_runtime.connectors.registry")
    monkeypatch.setattr(reg_mod, "_registry", reg_mod.ConnectorRegistry())
    channel = mods_channel.ModChannel()
    monkeypatch.setattr(mods_channel, "_channel", channel)
    return channel


def _install_mod(home, name="alpha", *, version="1.0.0"):
    mod_dir = home / "mods" / name
    (mod_dir / ".claude-plugin").mkdir(parents=True)
    (mod_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version})
    )
    hooks_dir = mod_dir / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(json.dumps({"modules": ["hooks.ts"]}))
    bridge_utils.update_mod_item(
        name, {"enabled": True, "source": "global", "origin": "test", "config": {}}
    )
    return mod_dir


def _report_mod(channel, name="alpha", *, status="loaded", version="1.0.0",
                user_config=None, schema=None):
    entry = {
        "name": name,
        "status": status,
        "version": version,
        "hooks": ["turn.start"],
        "userConfig": dict(DEFAULTS if user_config is None else user_config),
    }
    if schema is not None:
        entry["configSchema"] = schema
    channel._absorb_ready({"mods": [entry]})


# ---- built-in connectors keep the old contract ------------------------------


def test_builtin_detail_still_has_config_defaults_effective(client):
    r = client.get("/api/connectors/chrome-extension")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "builtin"
    assert body["configSchema"]["properties"]
    assert body["defaults"]["tab_scope"] == "group"
    # 设计 §3.2:config = 用户覆盖,effective = defaults ∪ stored。
    assert body["config"] == {}
    assert body["effective"] == body["defaults"]
    assert body["effective"]["fallback_profile_mode"] == "ask"
    assert body["overrides"] == {}
    assert body["detailView"]["tabs"]
    assert "settings" in [tab["kind"] for tab in body["detailView"]["tabs"]]
    # 列表快照新增字段
    listed = {c["id"]: c for c in client.get("/api/connectors").json()["connectors"]}
    assert listed["chrome-extension"]["kind"] == "builtin"
    assert listed["chrome-extension"]["configSummary"]["count"] >= 1


def test_builtin_with_only_enabled_has_no_settings_tab(client):
    body = client.get("/api/connectors/vibe-island").json()
    kinds = [tab["kind"] for tab in body["detailView"]["tabs"]]
    assert "settings" not in kinds
    assert "overview" in kinds and "activity" in kinds


# ---- mod connector -----------------------------------------------------------


def test_installed_mod_appears_as_connector_with_reported_defaults(
    client, isolated_home, fresh_connector_state
):
    _install_mod(isolated_home)
    _report_mod(fresh_connector_state)

    payload = client.get("/api/connectors").json()
    conns = {c["id"]: c for c in payload["connectors"]}
    mod = conns["mod-alpha"]
    assert mod["kind"] == "mod"
    assert mod["name"] == "alpha"
    assert mod["status"] == "connected"  # loaded → connected
    assert mod["version"] == "1.0.0"
    assert mod["configSummary"]["count"] == len(DEFAULTS)
    assert mod["configSummary"]["modified"] == 0
    assert mod["extra"]["modName"] == "alpha"
    assert mod["extra"]["source"] == "global"
    assert "overview" in [t["kind"] for t in mod["detailView"]["tabs"]]
    assert "permissions" in [t["kind"] for t in mod["detailView"]["tabs"]]

    detail = client.get("/api/connectors/mod-alpha").json()
    assert detail["defaults"] == DEFAULTS
    assert detail["effective"] == DEFAULTS
    assert detail["overrides"] == {}
    assert detail["classicGrant"] == "ask"
    props = detail["configSchema"]["properties"]
    assert props["units"]["type"] == "string"
    assert props["units"]["default"] == "metric"
    assert props["retries"]["type"] == "integer"
    assert props["ratio"]["type"] == "number"
    assert props["verbose"]["type"] == "boolean"
    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"]["type"] == "string"
    # dict 值不生成表单控件(高级 JSON 逃生口)
    assert "advanced" not in props
    assert "settings" in [t["kind"] for t in detail["detailView"]["tabs"]]


def test_reported_schema_passthrough_and_defaults(
    client, isolated_home, fresh_connector_state
):
    """mod 显式声明的 schema 原样透传 x-ginno-* 扩展;缺失的默认值补上,
    未覆盖的默认值键仍能被推断出来。"""
    schema = {
        "type": "object",
        "properties": {
            "units": {
                "type": "string",
                "enum": ["metric", "imperial"],
                "x-ginno-group": "Display",
                "x-ginno-order": 10,
                "x-ginno-secret": True,
                "x-ginno-placeholder": "metric",
                "x-ginno-min": 0,
                "x-ginno-max": 2,
            }
        },
    }
    _install_mod(isolated_home)
    _report_mod(fresh_connector_state, schema=schema)
    props = client.get("/api/connectors/mod-alpha").json()["configSchema"]["properties"]
    assert props["units"]["enum"] == ["metric", "imperial"]
    assert props["units"]["x-ginno-group"] == "Display"
    assert props["units"]["x-ginno-order"] == 10
    assert props["units"]["x-ginno-secret"] is True
    assert props["units"]["x-ginno-placeholder"] == "metric"
    assert props["units"]["x-ginno-min"] == 0
    assert props["units"]["x-ginno-max"] == 2
    assert props["units"]["default"] == "metric"
    # schema 没声明的默认值键:补推断,否则 UI 看不到。
    assert props["retries"]["type"] == "integer"


def test_mod_patch_writes_settings_mods_not_connectors(
    client, isolated_home, fresh_connector_state
):
    _install_mod(isolated_home)
    _report_mod(fresh_connector_state)

    r = client.patch(
        "/api/connectors/mod-alpha/config", json={"units": "imperial"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["config"]["units"] == "imperial"
    # 频道未连接 → 配置已存,但重载明确报告未发生(不谎称已应用)。
    assert body["applied"]["reloaded"] is False
    assert "not connected" in body["applied"]["error"]

    items = bridge_utils.load_mods_settings()["items"]
    assert items["alpha"]["config"] == {"units": "imperial"}
    raw = json.loads((isolated_home / "settings.json").read_text())
    assert "mod-alpha" not in (raw.get("connectors") or {})

    detail = client.get("/api/connectors/mod-alpha").json()
    assert detail["overrides"] == {"units": "imperial"}
    assert detail["effective"]["units"] == "imperial"
    # 默认值仍然覆盖未改动的键
    assert detail["effective"]["retries"] == 3


def test_mod_enable_toggle_writes_item_not_config(
    client, isolated_home, fresh_connector_state
):
    """卡片/抽屉的启停按钮走 PATCH config {enabled} —— mod 的开关属于
    settings item,不能混进 userConfig 配置块。"""
    _install_mod(isolated_home)
    _report_mod(fresh_connector_state)
    r = client.patch("/api/connectors/mod-alpha/config", json={"enabled": False})
    assert r.status_code == 200
    item = bridge_utils.load_mods_settings()["items"]["alpha"]
    assert item["enabled"] is False
    assert item.get("config") == {}
    assert _listed(client)["mod-alpha"]["enabled"] is False


def test_mod_patch_rejects_truly_unknown_keys(
    client, isolated_home, fresh_connector_state
):
    _install_mod(isolated_home)
    _report_mod(fresh_connector_state)
    r = client.patch("/api/connectors/mod-alpha/config", json={"not_a_key": 1})
    assert r.status_code == 400
    assert "unknown config keys" in r.json()["error"]
    assert bridge_utils.load_mods_settings()["items"]["alpha"].get("config") == {}


def test_unknown_builtin_key_still_rejected(client):
    r = client.patch(
        "/api/connectors/chrome-extension/config", json={"not_a_key": 1}
    )
    assert r.status_code == 400


# ---- sync: status mapping + unregister --------------------------------------


def test_status_mapping_and_unregister(
    client, isolated_home, fresh_connector_state
):
    mod_dir = _install_mod(isolated_home)
    _report_mod(fresh_connector_state, status="loaded")
    assert _listed(client)["mod-alpha"]["status"] == "connected"

    fresh_connector_state.mods_state["alpha"]["status"] = "restarting"
    conn = _listed(client)["mod-alpha"]
    assert conn["status"] == "restarting"
    # restarting 走 warn 聚合,不是 error
    assert client.get("/api/connectors").json()["aggregateDot"] == "warn"

    # 卸载:目录 + settings item + broker 状态全清 → 适配器注销
    import shutil

    shutil.rmtree(mod_dir)
    bridge_utils.remove_mod_item("alpha")
    fresh_connector_state.mods_state.pop("alpha", None)
    fresh_connector_state.mod_configs.pop("alpha", None)
    assert "mod-alpha" not in _listed(client)
    reg_mod = importlib.import_module("ginno_runtime.connectors.registry")
    assert reg_mod.registry().get("mod-alpha") is None
    # 注册闸门:未注册的 mod id 不会回落 enabled=True
    assert reg_mod.registry().read_config("mod-alpha")["enabled"] is False


def test_disabled_mod_maps_to_disabled(client, isolated_home, fresh_connector_state):
    _install_mod(isolated_home)
    _report_mod(fresh_connector_state, status="loaded")
    bridge_utils.update_mod_item("alpha", {"enabled": False})
    assert _listed(client)["mod-alpha"]["status"] == "disabled"


def _listed(client) -> dict:
    return {c["id"]: c for c in client.get("/api/connectors").json()["connectors"]}