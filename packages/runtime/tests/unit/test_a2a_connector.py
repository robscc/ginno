"""A2A 连接器（connectors/a2a.py）单测。

隔离范式照 test_connectors_deny.py：独立 ConnectorRegistry 实例，不碰全局
registry()；PeerStore/ContextMap 用 tmp_path 注入；settings_path monkeypatch
到 tmp（枢纽基类存储隔离）。
"""

from __future__ import annotations

import sys

import pytest

from ginno_runtime import paths
from ginno_runtime.a2a.peers import PeerRecord, PeerStore
from ginno_runtime.connectors import a2a as a2a_module
from ginno_runtime.connectors.a2a import (
    A2AHubConnector,
    A2APeerConnector,
    delete_peer,
    reconnect_peer,
    sync_peer_connectors,
)
from ginno_runtime.connectors.registry import (
    STATUS_CONNECTED,
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
    # 记录元数据不是配置覆盖：混出去会被 ConfigEditor 当未知键 PATCH 回来
    # 撞接口白名单 400（2026-11-18 e2e 实测）。
    for meta in ("slug", "id", "name", "created_at"):
        assert meta not in cfg
        assert meta not in reg.get("a2a-expense-bot").read_stored_config()


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

# ---- 2026-11-18 改造：peer 收进 hub 子配置 ----------------------------------
#
# peer 实例不再占顶层卡位（hide_in_list），枢纽详情新增 kind="a2a-peers"
# 页签；删除/重连作为 hub 上的管理 action。隔离纪律：_probe_results 是模块
# 级缓存，autouse 清空防跨用例泄漏；delete/reconnect 实现体引用全局
# registry()/ensure_a2a_hub，测试里分别 monkeypatch 到隔离实例。

@pytest.fixture(autouse=True)
def _clear_probe_cache():
    a2a_module._probe_results.clear()
    yield
    a2a_module._probe_results.clear()


@pytest.fixture()
def peer_paths(tmp_path, monkeypatch):
    """默认路径 PeerStore()/ContextMap() 注入到 tmp——delete/reconnect
    实现体内部自建 PeerStore()，与注入 store 指向同一文件。"""
    peers = tmp_path / "peers.json"
    contexts = tmp_path / "contexts.json"
    monkeypatch.setattr(paths, "a2a_peers_path", lambda: peers)
    monkeypatch.setattr(paths, "a2a_contexts_path", lambda: contexts)
    return PeerStore(peers, contexts_path=contexts)


def test_hub_detail_view_has_peers_tab(monkeypatch):
    monkeypatch.setenv("GINNO_LANGUAGE", "zh-CN")
    view = A2AHubConnector().detail_view()
    assert [t["kind"] for t in view["tabs"]] == [
        "overview", "a2a-peers", "settings", "activity"]
    peers_tab = view["tabs"][1]
    assert peers_tab["id"] == "peers"
    assert peers_tab["title"] == "对端节点"

    monkeypatch.setenv("GINNO_LANGUAGE", "en")
    assert A2AHubConnector().detail_view()["tabs"][1]["title"] == "Peer nodes"


def test_peer_instance_hidden_from_list(reg, store):
    """peer 实例 hide_in_list：顶层列表不出现，详情/配置 API 仍可解析。"""
    assert A2APeerConnector.hide_in_list is True
    assert A2AHubConnector.hide_in_list is False
    reg.register(A2AHubConnector())
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    payload = reg.list_payload()
    ids = [c["id"] for c in payload["connectors"]]
    assert "a2a" in ids
    assert "a2a-expense-bot" not in ids
    # 隐藏≠注销：详情/配置 PATCH 的解析路径不受影响
    assert reg.get("a2a-expense-bot") is not None
    snap = reg.status_of("a2a-expense-bot")
    assert snap["status"] == STATUS_DISCONNECTED
    # registry 全量里仍在（sync 幂等逻辑依赖）
    assert [c.id for c in reg.all()] == ["a2a", "a2a-expense-bot"]


def test_hidden_peer_exempt_from_aggregate_dot(reg, store):
    """peer 的 disconnected 不再把 sidebar 聚合点打成 warn。"""
    reg.register(A2AHubConnector())
    store.add(_peer())
    reg.report("a2a", STATUS_CONNECTED, "ok")
    sync_peer_connectors(reg, store=store)
    assert reg.aggregate_dot() == ""
    # 反证：没有豁免时 peer disconnected 必然 warn（钉住回归目标）
    reg.report("a2a-expense-bot", STATUS_DISCONNECTED, "x", force=True)
    assert reg.aggregate_dot() == ""


def test_delete_peer_ok_unregisters_instance(reg, store, peer_paths,
                                              monkeypatch):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    # 实现体尾部的 ensure_a2a_hub 走全局 registry——替换为手动同步，隔离。
    monkeypatch.setattr(a2a_module, "ensure_a2a_hub",
                        lambda *a, **k: sync_peer_connectors(reg, store=store))
    r = delete_peer({"slug": "expense-bot"})
    assert r == {"ok": True, "removed": True}
    assert store.find("expense-bot") is None
    assert reg.get("a2a-expense-bot") is None  # 动态实例同步注销
    assert not a2a_module._probe_results


def test_delete_peer_unknown_and_missing_slug(peer_paths):
    assert delete_peer({})["ok"] is False
    assert delete_peer({"slug": "ghost"})["ok"] is False


def test_delete_peer_pending_task_guard(store, peer_paths, monkeypatch):
    from ginno_runtime.a2a.contexts import ContextMap, ContextRecord, TaskBrief

    store.add(_peer())
    ctx = ContextMap(peer_paths.path.parent / "contexts.json")
    ctx.set(ContextRecord(ginno_sid="s1", peer_slug="expense-bot",
                          context_id="C1",
                          tasks=[TaskBrief(task_id="T1", state="working")]))
    monkeypatch.setattr(a2a_module, "ensure_a2a_hub",
                        lambda *a, **k: None)
    r = delete_peer({"slug": "expense-bot"})
    assert r["ok"] is False
    assert r["pending"][0]["task_id"] == "T1"
    assert store.find("expense-bot") is not None  # 未删


async def test_reconnect_peer_probe_ok(reg, store, peer_paths, monkeypatch):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    # 包 __init__ 把 registry 函数 re-export 出来遮蔽了同名子模块属性,
    # 点路径 monkeypatch 会打到函数上——从 sys.modules 拿真模块。
    registry_mod = sys.modules["ginno_runtime.connectors.registry"]
    monkeypatch.setattr(registry_mod, "registry", lambda: reg)
    seen: dict = {}

    async def fake_fetch(endpoint, *, auth=None, verify_fp=None,
                         timeout_s=0.0, allow_private=False, **kw):
        seen.update(endpoint=endpoint, auth=auth, verify_fp=verify_fp,
                    allow_private=allow_private)
        return object()

    monkeypatch.setattr("ginno_runtime.a2a.card.fetch_card", fake_fetch)
    r = await reconnect_peer({"slug": "expense-bot"})
    assert r == {"ok": True, "status": "connected", "error": None}
    assert seen["endpoint"] == "https://bot.example/a2a"
    assert seen["allow_private"] is True
    assert reg.status_of("a2a-expense-bot")["status"] == STATUS_CONNECTED
    # 探针结果跨 sync 保留（否则每次列表刷新都打回 disconnected）
    sync_peer_connectors(reg, store=store)
    assert reg.status_of("a2a-expense-bot")["status"] == STATUS_CONNECTED


async def test_reconnect_peer_failure_scrubs_token(reg, store, peer_paths,
                                                   monkeypatch):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    # 包 __init__ 把 registry 函数 re-export 出来遮蔽了同名子模块属性,
    # 点路径 monkeypatch 会打到函数上——从 sys.modules 拿真模块。
    registry_mod = sys.modules["ginno_runtime.connectors.registry"]
    monkeypatch.setattr(registry_mod, "registry", lambda: reg)

    async def boom(endpoint, **kw):
        raise ValueError(f"connect failed: secret-token-12345678")

    monkeypatch.setattr("ginno_runtime.a2a.card.fetch_card", boom)
    r = await reconnect_peer({"slug": "expense-bot"})
    assert r["ok"] is False
    assert r["status"] == STATUS_DISCONNECTED
    assert "secret-token" not in r["error"]
    assert "••••" in r["error"]
    assert reg.status_of("a2a-expense-bot")["status"] == STATUS_DISCONNECTED


def test_sync_disabled_overrides_probe_cache(reg, store):
    store.add(_peer())
    sync_peer_connectors(reg, store=store)
    a2a_module._probe_results["expense-bot"] = (STATUS_CONNECTED, "Connected")
    store.update("expense-bot", {"enabled": False})
    sync_peer_connectors(reg, store=store)
    assert reg.status_of("a2a-expense-bot")["status"] == STATUS_DISABLED
