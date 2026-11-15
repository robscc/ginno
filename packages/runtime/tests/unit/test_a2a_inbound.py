"""Unit tests for a2a.inbound —— 绑定解析 / 惰性 identity / 能力地址 /
真实 uvicorn 起停幂等（设计 §6.3 / §10.1）。

uvicorn 用本机随机端口（port=0）真实起停；lan 档枚举用 monkeypatch 打桩。
"""

from __future__ import annotations

import stat

import httpx
import pytest

from ginno_runtime.a2a import address, inbound

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_handles():
    inbound._HANDLES.clear()
    yield
    inbound._HANDLES.clear()


# -- 绑定三档解析（绝不 0.0.0.0）----------------------------------------------


def test_bind_loopback_default():
    assert inbound.InboundConfig().bind_mode == "loopback"
    assert inbound.resolve_bind("loopback") == "127.0.0.1"


def test_bind_custom_explicit_ip():
    assert inbound.resolve_bind("custom", "192.168.1.5") == "192.168.1.5"


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "224.0.0.1", "not-an-ip", ""])
def test_bind_custom_rejects_wildcard_and_garbage(bad):
    with pytest.raises(ValueError):
        inbound.resolve_bind("custom", bad)


def test_bind_lan_picks_private_address(monkeypatch):
    monkeypatch.setattr(
        inbound, "list_lan_addresses", lambda: ["192.168.1.5", "10.0.0.9"]
    )
    assert inbound.resolve_bind("lan") == "192.168.1.5"


def test_bind_lan_fail_closed_when_no_private(monkeypatch):
    monkeypatch.setattr(inbound, "list_lan_addresses", lambda: [])
    with pytest.raises(ValueError):
        inbound.resolve_bind("lan")  # 绝不回退 wildcard


def test_list_lan_addresses_never_wildcard():
    for addr in inbound.list_lan_addresses():
        assert addr not in ("0.0.0.0", "::")
        assert not addr.startswith("127.")


def test_unknown_bind_mode_rejected():
    with pytest.raises(ValueError):
        inbound.resolve_bind("everything")
    with pytest.raises(ValueError):
        inbound.ensure_inbound_listener(inbound.InboundConfig(bind_mode="everything"))


# -- 能力地址与惰性 identity --------------------------------------------------


def test_capability_address_format_and_fp(tmp_path):
    cfg = inbound.InboundConfig(data_dir=tmp_path, port=8790)
    handle = inbound.ensure_inbound_listener(cfg)
    addr = handle.capability_address()
    assert addr.startswith("https://127.0.0.1:8790/a2a/")
    assert "#fp=" in addr
    parsed = address.parse_address(addr)
    assert parsed.token == handle.token
    assert parsed.fingerprint == handle.get_identity().fingerprint
    assert len(parsed.token) == 26
    assert len(parsed.fingerprint) == 26
    # P0 环回自测显式 http；scheme 参数已预留
    assert handle.capability_address(scheme="http").startswith(
        "http://127.0.0.1:8790/a2a/"
    )


def test_identity_lazy_and_key_0600(tmp_path):
    handle = inbound.ensure_inbound_listener(inbound.InboundConfig(data_dir=tmp_path))
    # ensure 之后尚未生成——惰性
    assert not (tmp_path / "inbound-cert.pem").exists()
    assert not (tmp_path / "inbound-key.pem").exists()
    handle.capability_address()
    key = tmp_path / "inbound-key.pem"
    assert key.exists()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    fp1 = handle.get_identity().fingerprint
    # 新 handle 从磁盘加载同一 identity（指纹稳定，pin 不漂移）
    inbound._HANDLES.clear()
    handle2 = inbound.ensure_inbound_listener(inbound.InboundConfig(data_dir=tmp_path))
    assert handle2.get_identity().fingerprint == fp1


def test_token_stable_across_restart_and_0600(tmp_path):
    cfg = inbound.InboundConfig(data_dir=tmp_path)
    h1 = inbound.ensure_inbound_listener(cfg)
    inbound._HANDLES.clear()  # 模拟进程重启
    h2 = inbound.ensure_inbound_listener(cfg)
    assert h2.token == h1.token
    assert stat.S_IMODE((tmp_path / "inbound-token").stat().st_mode) == 0o600


def test_ensure_returns_same_handle(tmp_path):
    cfg = inbound.InboundConfig(data_dir=tmp_path)
    assert inbound.ensure_inbound_listener(cfg) is inbound.ensure_inbound_listener(cfg)


# -- 真实 uvicorn 起停（随机端口）----------------------------------------------


async def test_start_stop_idempotent_real_listener(tmp_path):
    cfg = inbound.InboundConfig(data_dir=tmp_path, port=0)
    handle = inbound.ensure_inbound_listener(cfg)
    assert not handle.running
    await handle.start()
    try:
        assert handle.running
        port = handle.actual_port
        assert port and port > 0
        await handle.start()  # 幂等：重复 start 不另起
        assert handle.running
        assert f":{port}/" in handle.capability_address(scheme="http")
        async with httpx.AsyncClient() as c:
            ok = await c.get(
                f"http://127.0.0.1:{port}/a2a/{handle.token}"
                "/.well-known/agent-card.json"
            )
            assert ok.status_code == 200
            denied = await c.get(f"http://127.0.0.1:{port}/a2a/")
            assert denied.status_code == 401
    finally:
        await handle.stop()
    assert not handle.running
    await handle.stop()  # 幂等：重复 stop 不抛
    assert not handle.running


async def test_restart_after_stop(tmp_path):
    handle = inbound.ensure_inbound_listener(
        inbound.InboundConfig(data_dir=tmp_path, port=0)
    )
    await handle.start()
    await handle.stop()
    await handle.start()  # 停后可再起
    try:
        assert handle.running
        assert handle.actual_port and handle.actual_port > 0
        async with httpx.AsyncClient() as c:
            resp = await c.get(
                f"http://127.0.0.1:{handle.actual_port}/a2a/{handle.token}"
                "/.well-known/agent-card.json"
            )
            assert resp.status_code == 200
    finally:
        await handle.stop()


async def test_tls_params_wired_with_generated_identity(tmp_path):
    """P1 的 TLS 路径已预留：ssl_certfile/ssl_keyfile 直通 uvicorn，
    用惰性生成的自签证书真实起 HTTPS（P0 默认 None = 明文，见上一组）。"""
    cfg = inbound.InboundConfig(data_dir=tmp_path, port=0)
    handle = inbound.ensure_inbound_listener(
        cfg,
        ssl_certfile=str(tmp_path / "inbound-cert.pem"),
        ssl_keyfile=str(tmp_path / "inbound-key.pem"),
    )
    handle.get_identity()  # 先落证书/私钥，再起 TLS
    await handle.start()
    try:
        port = handle.actual_port
        async with httpx.AsyncClient(verify=False) as c:
            resp = await c.get(
                f"https://127.0.0.1:{port}/a2a/{handle.token}"
                "/.well-known/agent-card.json"
            )
            assert resp.status_code == 200
    finally:
        await handle.stop()
