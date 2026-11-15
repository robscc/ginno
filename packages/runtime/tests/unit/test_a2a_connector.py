"""A2A 连接器（connectors/a2a.py）单测。

隔离范式照 test_connectors_deny.py：独立 ConnectorRegistry 实例，不碰全局
registry()；PeerStore/ContextMap 用 tmp_path 注入；settings_path monkeypatch
到 tmp（枢纽基类存储隔离）。
"""

from __future__ import annotations

import pytest

from ginno_runtime import paths
from ginno_runtime.a2a.peers import PeerRecord, PeerStore
from ginno_runtime.connectors.a2a import (
    A2AHubConnector,
    A2APeerConnector,
    sync_peer_connectors,
)
from ginno_runtime.connectors.registry import (
    STATUS_DISABLED,
    STATUS_DISCONNECTED,
    ConnectorRegistry,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "settings_path",
                        lambda: tmp_path / "settings.json")


@pytest.fixture()
def reg():
    return ConnectorRegistry()


@pytest.fixture()
def store(tmp_path):
    return PeerStore(tmp_path / "peers.json",
                     contexts_path=tmp_path / "contexts.json")


def _peer(slug="expense-bot", **kw):
    defaults = dict(name="报销助手", endpoint="https://bot.example/a2a",
                    token="secret-token-12345678")
    defaults.update(kw)
    return PeerRecord(slug=slug, **defaults)


# ---- 枢纽 --------------------------------------------------------------------


def test_hub_identity(reg):
    hub = A2AHubConnector()
    reg.register(hub)
    assert reg.get("a2a") is hub
    assert hub.name == "A2A · 外部 Agent 协作"
    assert hub.icon == "network"
    assert hub.order == 90
    assert hub.kind == "builtin"
    assert hub.dynamic is False
    assert hub.tool_prefix is None


def test_hub_default_config(reg):
    hub = A2AHubConnector()
    reg.register(hub)
    cfg = reg.read_config("a2a")
    assert cfg["a2a_enabled"] is False
    assert cfg["inbound_enabled"] is False
    assert cfg["bind_mode"] == "loopback"
    assert cfg["port"] == 8790
    assert cfg["admin_enabled"] is False
    assert cfg["audit_enabled"] is True


def test_hub_config_schema_shape():
    props = A2AHubConnector().config_schema()["properties"]
    assert set(props) == {"a2a_enabled", "inbound_enabled", "bind_mode",
                          "bind_interface", "port", "admin_enabled",
                          "audit_enabled"}
    assert props["bind_mode"]["enum"] == ["loopback", "lan", "custom"]
    assert props["port"]["type"] == "integer"
    assert props["a2a_enabled"]["type"] == "boolean"
    for prop in props.values():
        assert "title" in prop


async def test_hub_apply_config_noop(reg):
    hub = A2AHubConnector()
    reg.register(hub)
    assert await hub.apply_config(reg.read_config("a2a")) is None


# ---- 实例 schema -------------------------------------------------------------


def test_peer_config_schema_password_and_enums(store):
    conn = A2APeerConnector(_peer(), store=store)
    props = conn.config_schema()["properties"]
    assert props["token"]["format"] == "password"
    assert props["trust"]["enum"] == ["readonly", "normal", "full"]
    assert props["auth_scheme"]["enum"] == ["none", "bearer", "api_key",
                                            "oauth2"]
    assert props["confirm_mode"]["enum"] == ["every", "after_first", "always"]
    assert props["caps"]["type"] == "array"
    assert props["task_timeout_s"]["type"] == "integer"


# ---- sync --------------------------------------------------------------------


def test_sync_empty_store(reg, store):
    assert sync_peer_connectors(reg, store=store) == []
    assert reg.all() == []


def test_sync_registers_peers(reg, store):
    store.add(_peer(slug="expense-bot"))
    store.add(_peer(slug="legal-review", name="法务审查"))
    ids = sync_peer_connectors(reg, store=store)
    assert ids == ["a2a-expense-bot", "a2a-legal-review"]
    conn = reg.get("a2a-expense-bot")
    assert isinstance(conn, A2APeerConnector)
    assert conn.dynamic is True
    assert conn.kind == "a2a"
    assert conn.name == "报销助手"
    assert conn.tool_prefix == "a2a_expense_bot_"
    assert conn.order >= 200
    assert reg.status_of("a2a-expense-bot")["status"] == STATUS_DISCONNECTED


def test_sync_idempotent_keeps_instance(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    first = reg.get("a2a-expense-bot")
    sync_peer_connectors(reg, store=store)
    assert reg.get("a2a-expense-bot") is first  # 刷新而非重建
    assert len(reg.all()) == 1


def test_sync_unregisters_removed_peer(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    store.remove("expense-bot")
    assert sync_peer_connectors(reg, store=store) == []
    assert reg.get("a2a-expense-bot") is None
    # 注册闸门:旧 id 不回落 enabled=True
    assert reg.read_config("a2a-expense-bot") == {"enabled": False}


def test_sync_disabled_peer_status(reg, store):
    store.add(_peer(enabled=False))
    sync_peer_connectors(reg, store=store)
    st = reg.status_of("a2a-expense-bot")
    assert st["status"] == STATUS_DISABLED
    assert st["enabled"] is False


def test_sync_refreshes_metadata(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    store.update("expense-bot", {"name": "新名字"})
    sync_peer_connectors(reg, store=store)
    assert reg.get("a2a-expense-bot").name == "新名字"


# ---- 实例配置存储（PeerStore,不是 settings.connectors）------------------------


def test_peer_read_stored_config_masks_token(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    cfg = reg.read_config("a2a-expense-bot")
    assert cfg["token"] == "••••5678"  # SEC-01:GET 只见掩码
    assert cfg["endpoint"] == "https://bot.example/a2a"
    assert cfg["trust"] == "readonly"
    assert cfg["enabled"] is True


def test_peer_write_stored_config_persists_to_peerstore(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    reg.write_config("a2a-expense-bot", {"enabled": False, "trust": "normal"})
    rec = store.find("expense-bot")
    assert rec.enabled is False
    assert rec.trust == "normal"
    # settings.connectors 里不该有影子配置
    assert not paths.settings_path().exists() or \
        "a2a-expense-bot" not in paths.settings_path().read_text()


def test_peer_write_masked_token_not_persisted(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    reg.write_config("a2a-expense-bot", {"token": "••••5678"})
    assert store.find("expense-bot").token == "secret-token-12345678"


async def test_peer_apply_config_shape(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    conn = reg.get("a2a-expense-bot")
    result = await conn.apply_config({"enabled": True})
    assert result == {"ok": True, "reloaded": False, "error": None}


# ---- detail_view / 工具闸 -----------------------------------------------------


def test_peer_detail_view_four_tabs(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    view = reg.get("a2a-expense-bot").detail_view()
    assert [t["id"] for t in view["tabs"]] == [
        "overview", "settings", "permissions", "activity"]
    assert all(set(t) == {"id", "kind", "title"} for t in view["tabs"])
    assert view["actions"] == []


def test_tool_denied_per_peer(reg, store):
    store.add(_peer(slug="expense-bot"))
    store.add(_peer(slug="legal-review"))
    sync_peer_connectors(reg, store=store)
    tool = "a2a_expense_bot_send_task"
    assert reg.tool_denied(tool, ["a2a-expense-bot"]) is True
    assert reg.tool_denied(tool, ["a2a-legal-review"]) is False
    assert reg.tool_denied(tool, []) is False


def test_hub_and_peers_coexist_ordering(reg, store):
    reg.register(A2AHubConnector())
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    ids = [c.id for c in reg.all()]
    assert ids == ["a2a", "a2a-expense-bot"]  # order 90 < 200