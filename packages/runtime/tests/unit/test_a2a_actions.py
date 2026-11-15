"""A2A 管理 action 单测（api/connectors.py 三个 a2a 分支 + 枢纽 extra 上报）。

打法照 test_a2a_server.py：httpx.AsyncClient + ASGITransport 打真
api/connectors.py 的 router（FastAPI 壳），分发器/ensure_builtin_connectors
全走真代码。隔离范式照 test_a2a_connector.py：
- 根 conftest 的 autouse isolated_home 已把 GINNO_HOME 指到 tmp_path；
- 全局 connector registry 每测试换新实例（monkeypatch _registry）；
- fetch_card 一律 monkeypatch 打桩——**零真网络**。

契约锚点：A2aPeerAddModal.tsx / A2aInboundCard.tsx 头注释。
"""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi import FastAPI

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime.a2a import address as a2a_address
from ginno_runtime.a2a import card as card_mod
from ginno_runtime.a2a.auth import TokenStore
from ginno_runtime.a2a.errors import A2AError
from ginno_runtime.a2a.models import AgentCard
from ginno_runtime.a2a.peers import PeerStore
from ginno_runtime.connectors.a2a import (
    A2APeerConnector,
    ensure_a2a_hub,
)

# connectors 包 re-export 了 registry() 函数，遮蔽同名子模块——
# 照 test_browser_connectors.py 的先例走 importlib 拿真模块。
import importlib

reg_mod = importlib.import_module("ginno_runtime.connectors.registry")

pytestmark = pytest.mark.unit

# 26 字符 base32（A-Z2-7）——能力地址 token / 指纹的合法形态。
TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
FP = "ZYXWVUTSRQPONMLKJIHGFEDCBA"
SECRET = "secret-peer-token-123456"

CARD_RAW = {
    "name": "Expense Bot",
    "description": "报销助手",
    "url": "https://bot.example/a2a",
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "capabilities": {"streaming": False},
    "skills": [{"id": "expense", "name": "Expense", "description": "d",
                "tags": ["finance"]}],
    "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
}


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    """全局 registry 每测试换新（api 分发器经 registry() 读同一全局）。"""
    monkeypatch.setattr(reg_mod, "_registry", reg_mod.ConnectorRegistry())


@pytest.fixture()
def app():
    from ginno_runtime.api.connectors import router

    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture()
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://conn.test"
    ) as c:
        yield c


@pytest.fixture()
def fetch_stub(monkeypatch):
    """fetch_card 打桩：记录调用参数；可注入返回 Card 或异常。"""
    holder = {"calls": [], "card": AgentCard.from_dict(CARD_RAW), "error": None}

    async def _fake(endpoint, *, auth=None, verify_fp=None, timeout_s=20.0,
                    http_client=None, allow_private=False):
        holder["calls"].append({
            "endpoint": endpoint, "verify_fp": verify_fp,
            "allow_private": allow_private, "timeout_s": timeout_s,
        })
        if holder["error"] is not None:
            raise holder["error"]
        return holder["card"]

    monkeypatch.setattr(card_mod, "fetch_card", _fake)
    return holder


async def _action(client, action, cid="a2a", **payload):
    resp = await client.post(f"/api/connectors/{cid}/action",
                             json={"action": action, **payload})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _enable_inbound(**overrides):
    """直接写 settings.connectors.a2a（等价于用户 PATCH 过的持久态）。"""
    cfg = {"inbound_enabled": True, **overrides}
    paths.settings_path().write_text(
        json.dumps({"connectors": {"a2a": cfg}}), encoding="utf-8")


def _store():
    return PeerStore(paths.a2a_peers_path(),
                     contexts_path=paths.a2a_contexts_path())


# ---- preview_card -------------------------------------------------------------


async def test_preview_success(client, fetch_stub):
    r = await _action(client, "a2a_preview_card", url="https://bot.example/a2a")
    assert r["ok"] is True
    assert r["card"]["name"] == "Expense Bot"
    assert r["card"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert r["missing"] == []
    assert r["private_network"] is False
    call = fetch_stub["calls"][0]
    assert call["allow_private"] is True  # 用户显式粘贴 → 放行私网
    assert call["timeout_s"] == 20.0


async def test_preview_missing_fields(client, fetch_stub):
    fetch_stub["card"] = AgentCard.from_dict({"name": "半张卡"})
    r = await _action(client, "a2a_preview_card", url="https://bot.example")
    assert r["ok"] is True
    assert set(r["missing"]) == {"url", "description", "version",
                                 "capabilities", "skills"}


async def test_preview_private_network_allowed_and_flagged(client, fetch_stub):
    url = f"http://192.168.1.5:8790/a2a/{TOKEN}#fp={FP}"
    r = await _action(client, "a2a_preview_card", url=url)
    assert r["ok"] is True  # 显式配置的内网 peer 放行（设计 §10.4）
    assert r["private_network"] is True  # 但前端要出信任提示
    assert len(fetch_stub["calls"]) == 1


async def test_preview_loopback_flagged(client, fetch_stub):
    r = await _action(client, "a2a_preview_card", url="http://127.0.0.1:8790")
    assert r["ok"] is True
    assert r["private_network"] is True


@pytest.mark.parametrize("url", [
    "ftp://bot.example/card",
    "javascript:alert(1)",
    "http://",
])
async def test_preview_ssrf_scheme_and_host_blocked(client, fetch_stub, url):
    """allow_private 只放宽私网判定，scheme/主机闸不放宽。"""
    r = await _action(client, "a2a_preview_card", url=url)
    assert r["ok"] is False
    assert r["error"]
    assert fetch_stub["calls"] == []  # 拦截发生在 fetch 之前


async def test_preview_fetch_http_error_degrades(client, fetch_stub):
    fetch_stub["error"] = A2AError(
        "Agent Card 获取失败：HTTP 404（对端可能未发布 Agent Card）")
    r = await _action(client, "a2a_preview_card", url="https://bot.example")
    assert r["ok"] is False
    assert "404" in r["error"]  # 前端据此走手动填表兜底


async def test_preview_network_error_scrubs_token(client, fetch_stub):
    # httpx 异常文本会带完整 URL（含 path token）——响应里必须掩码。
    fetch_stub["error"] = A2AError(
        "Agent Card 获取失败（网络错误）：[Errno 61] Connect call failed "
        f"('https://bot.example/a2a/{TOKEN}')")
    r = await _action(client, "a2a_preview_card",
                      url=f"https://bot.example/a2a/{TOKEN}")
    assert r["ok"] is False
    assert TOKEN not in r["error"]
    assert TOKEN not in json.dumps(r)
    assert "••••" in r["error"]


async def test_preview_invalid_capability_token(client, fetch_stub):
    r = await _action(client, "a2a_preview_card",
                      url=f"https://bot.example/a2a/{TOKEN[:25]}")
    assert r["ok"] is False
    assert "base32" in r["error"]
    assert fetch_stub["calls"] == []


async def test_preview_empty_url(client, fetch_stub):
    r = await _action(client, "a2a_preview_card", url="  ")
    assert r["ok"] is False
    assert fetch_stub["calls"] == []


async def test_preview_fp_pinning_only_for_https(client, fetch_stub):
    await _action(client, "a2a_preview_card",
                  url=f"https://bot.example/a2a/{TOKEN}#fp={FP}")
    assert fetch_stub["calls"][0]["verify_fp"] == FP
    await _action(client, "a2a_preview_card",
                  url=f"http://127.0.0.1:8790/a2a/{TOKEN}#fp={FP}")
    # P0 环回明文无证书可校 → 跳过 pinning（https 才校验）
    assert fetch_stub["calls"][1]["verify_fp"] is None


# ---- add_peer -----------------------------------------------------------------


async def test_add_peer_creates_instance(client):
    r = await _action(
        client, "a2a_add_peer",
        endpoint="https://bot.example/a2a", name="Expense Bot",
        peer_fingerprint=FP, auth_scheme="bearer", token=SECRET,
        trust="normal", caps=["L0", "L1"], confirm_mode="after_first")
    assert r == {"ok": True, "id": "a2a-expense-bot", "slug": "expense-bot"}
    conn = reg_mod.registry().get("a2a-expense-bot")
    assert isinstance(conn, A2APeerConnector)  # 实例立即出现
    rec = _store().find("expense-bot")
    assert rec.endpoint == "https://bot.example/a2a"
    assert rec.token == SECRET
    assert rec.peer_fingerprint == FP
    assert rec.trust == "normal"
    assert rec.caps == ["L0", "L1"]
    assert rec.confirm_mode == "after_first"
    assert reg_mod.registry().status_of("a2a-expense-bot")["status"] == \
        "disconnected"


async def test_add_peer_slug_conflict_suffix(client):
    p = {"endpoint": "https://bot.example/a2a", "name": "Expense Bot"}
    r1 = await _action(client, "a2a_add_peer", **p)
    r2 = await _action(client, "a2a_add_peer", **p)
    assert r1["slug"] == "expense-bot"
    assert r2["slug"] == "expense-bot-2"
    assert r2["id"] == "a2a-expense-bot-2"
    assert reg_mod.registry().get("a2a-expense-bot-2") is not None


async def test_add_peer_defaults_fail_closed(client):
    r = await _action(client, "a2a_add_peer",
                      endpoint="https://bot.example/a2a", name="Minimal")
    assert r["ok"] is True
    rec = _store().find(r["slug"])
    assert rec.trust == "readonly"
    assert rec.caps == ["L0"]
    assert rec.confirm_mode == "every"
    assert rec.auth_scheme == "none"


async def test_add_peer_invalid_enums_fail_closed(client):
    r = await _action(client, "a2a_add_peer",
                      endpoint="https://bot.example/a2a", name="Weird",
                      trust="yolo", confirm_mode="never", auth_scheme="basic")
    assert r["ok"] is True
    rec = _store().find(r["slug"])
    assert rec.trust == "readonly"
    assert rec.confirm_mode == "every"
    assert rec.auth_scheme == "none"


async def test_add_peer_capability_address_token_extraction(client):
    r = await _action(
        client, "a2a_add_peer",
        endpoint=f"https://bot.example/a2a/{TOKEN}#fp={FP}",
        name="Cap Peer", token="")
    assert r["ok"] is True
    rec = _store().find(r["slug"])
    assert rec.token == TOKEN  # 地址即凭据：空 token 自动从地址取
    assert rec.peer_fingerprint == FP  # #fp 同样自动取
    assert rec.endpoint == f"https://bot.example/a2a/{TOKEN}"  # 无 fragment


async def test_add_peer_chinese_name_slug_from_host(client):
    r = await _action(client, "a2a_add_peer",
                      endpoint="https://bot.example/a2a", name="报销助手")
    assert r["ok"] is True
    assert r["slug"] == "bot-example"  # 纯中文清洗为空 → 回落 host


async def test_add_peer_missing_endpoint(client):
    r = await _action(client, "a2a_add_peer", name="No Endpoint")
    assert r["ok"] is False
    assert "endpoint" in r["error"]


async def test_add_peer_masked_token_not_persisted(client):
    r = await _action(client, "a2a_add_peer",
                      endpoint="https://bot.example/a2a", name="Masked",
                      token="••••abcd")
    assert r["ok"] is True
    assert _store().find(r["slug"]).token == ""  # SEC-01：掩码不落盘


async def test_add_peer_response_has_no_token(client):
    resp = await client.post("/api/connectors/a2a/action", json={
        "action": "a2a_add_peer", "endpoint": "https://bot.example/a2a",
        "name": "Leak Check", "token": SECRET})
    assert resp.status_code == 200
    assert SECRET not in resp.text


# ---- reset_inbound_address ------------------------------------------------------


async def test_reset_rotates_token_old_invalid(client):
    _enable_inbound()
    ensure_a2a_hub()  # 首次派生值上报 → 签发初始 token
    old = (paths.a2a_dir() / "inbound-token").read_text().strip()
    r = await _action(client, "a2a_reset_inbound_address")
    assert r["ok"] is True
    new = (paths.a2a_dir() / "inbound-token").read_text().strip()
    assert new != old
    assert new in r["address"]
    tokens = TokenStore(paths.a2a_dir() / "tokens.json")
    assert tokens.verify(old) is False  # 旧地址立即失效
    assert tokens.verify(new) is True


async def test_reset_fingerprint_stable_identity_untouched(client):
    _enable_inbound()
    r1 = await _action(client, "a2a_reset_inbound_address")
    cert_before = (paths.a2a_dir() / "inbound-cert.pem").read_bytes()
    r2 = await _action(client, "a2a_reset_inbound_address")
    assert r1["ok"] is True and r2["ok"] is True
    fp1 = a2a_address.parse_address(r1["address"]).fingerprint
    fp2 = a2a_address.parse_address(r2["address"]).fingerprint
    assert fp1 == fp2  # identity 不变 → 对端无需重新配对
    assert r1["address"] != r2["address"]  # 但 token 换了
    assert (paths.a2a_dir() / "inbound-cert.pem").read_bytes() == cert_before


async def test_reset_audit_recorded_without_plaintext(client):
    _enable_inbound()
    ensure_a2a_hub()
    old = (paths.a2a_dir() / "inbound-token").read_text().strip()
    r = await _action(client, "a2a_reset_inbound_address")
    new = (paths.a2a_dir() / "inbound-token").read_text().strip()
    audit = (paths.a2a_dir() / "inbound-audit.jsonl").read_text()
    entries = [json.loads(line) for line in audit.splitlines() if line]
    assert any(e["method"] == "inbound_address_reset" for e in entries)
    assert old not in audit and new not in audit  # 只落 hash 前缀


async def test_reset_updates_extra(client):
    _enable_inbound()
    r = await _action(client, "a2a_reset_inbound_address")
    extra = reg_mod.registry().status_of("a2a")["extra"]
    assert extra["inboundAddress"] == r["address"]


async def test_reset_fresh_without_prior_token(client):
    # 入站从未启用过（无 token 文件）也能重置：直接签发新地址。
    r = await _action(client, "a2a_reset_inbound_address")
    assert r["ok"] is True
    parsed = a2a_address.parse_address(r["address"])
    assert parsed.is_capability
    assert TokenStore(paths.a2a_dir() / "tokens.json").verify(parsed.token)


# ---- extra 三字段（enabled / disabled 两态）-------------------------------------


async def test_extra_disabled_empty(client):
    ensure_a2a_hub()
    extra = reg_mod.registry().status_of("a2a")["extra"]
    assert extra == {"inboundAddress": "", "inboundFingerprint": "",
                     "listenAddress": ""}
    # 降级态不造假地址，也不惰性生成 identity/token
    assert not (paths.a2a_dir() / "inbound-cert.pem").exists()
    assert not (paths.a2a_dir() / "inbound-token").exists()


async def test_extra_enabled_shape(client):
    _enable_inbound()
    ensure_a2a_hub()
    extra = reg_mod.registry().status_of("a2a")["extra"]
    assert extra["listenAddress"] == "127.0.0.1:8790"
    assert extra["inboundAddress"].startswith("https://127.0.0.1:8790/a2a/")
    assert "#fp=" in extra["inboundAddress"]
    assert re.fullmatch(r"[A-Z2-7]{26}", extra["inboundFingerprint"])
    parsed = a2a_address.parse_address(extra["inboundAddress"])
    assert parsed.fingerprint == extra["inboundFingerprint"]
    assert (paths.a2a_dir() / "inbound-cert.pem").exists()  # 惰性生成落盘
    assert (paths.a2a_dir() / "inbound-key.pem").exists()


async def test_extra_custom_port(client):
    _enable_inbound(port=9999)
    ensure_a2a_hub()
    extra = reg_mod.registry().status_of("a2a")["extra"]
    assert extra["listenAddress"] == "127.0.0.1:9999"
    assert ":9999/a2a/" in extra["inboundAddress"]


async def test_extra_lan_fail_closed_degrades_empty(client, monkeypatch):
    from ginno_runtime.a2a import inbound as inbound_mod

    _enable_inbound(bind_mode="lan")
    monkeypatch.setattr(inbound_mod, "list_lan_addresses", lambda: [])
    ensure_a2a_hub()  # 找不到私网地址 → 不 raise、不造假，降级空态
    extra = reg_mod.registry().status_of("a2a")["extra"]
    assert extra == {"inboundAddress": "", "inboundFingerprint": "",
                     "listenAddress": ""}


async def test_patch_config_toggle_refreshes_extra(client):
    """前端开关流：PATCH inbound_enabled → apply_config 刷新 extra。"""
    ensure_a2a_hub()
    assert reg_mod.registry().status_of("a2a")["extra"]["inboundAddress"] == ""
    resp = await client.patch("/api/connectors/a2a/config",
                              json={"inbound_enabled": True})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    extra = reg_mod.registry().status_of("a2a")["extra"]
    assert extra["inboundAddress"].startswith("https://127.0.0.1:8790/a2a/")
    assert extra["listenAddress"] == "127.0.0.1:8790"


# ---- 既有分发行为不回归 ----------------------------------------------------------


async def test_unknown_action_still_400(client):
    resp = await client.post("/api/connectors/a2a/action",
                             json={"action": "nope"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown action: nope"}


async def test_confirm_domain_branch_unaffected(client):
    r = await _action(client, "confirm_domain", cid="chrome-extension",
                      domain="Pay.Example.com")
    assert r["ok"] is True
    assert r["confirmedDomains"] == ["pay.example.com"]


# ------------------- 冷启动注册（集成期修的鸡生蛋死锁）------------------- #


def test_cold_start_registers_a2a_hub():
    """ensure_builtin_connectors 必须注册 a2a 枢纽。

    集成期实测缺口：builtin.py 原先没挂接，冷启动列表里没有 a2a → 前端进不了
    详情页 → 管理 action 永远触发不到 → action 内的 ensure_a2a_hub() 兜底又
    只在 action 触发后才生效，形成鸡生蛋死锁。此测试钉死冷启动即可见。
    """
    from ginno_runtime.connectors.builtin import ensure_builtin_connectors
    from ginno_runtime.connectors.registry import registry

    ensure_builtin_connectors()
    ids = {c.id for c in registry().all()}
    assert "a2a" in ids, "A2A 枢纽必须在冷启动就注册，否则前端无法进入详情页"
    # 既有内置连接器不能被 A2A 的引入挤掉
    for legacy in ("chrome-extension", "browser-profile", "wecom-bot", "dingtalk-bot"):
        assert legacy in ids
