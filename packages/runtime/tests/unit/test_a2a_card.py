"""Agent Card 发现/校验 + SSRF 防护单测（httpx MockTransport，零真网络）。"""

from __future__ import annotations

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a import card
from ginno_runtime.a2a.address import fingerprint_of
from ginno_runtime.a2a.errors import A2AError
from ginno_runtime.a2a.models import AgentCard

pytestmark = pytest.mark.unit

_CARD_PAYLOAD = {
    "name": "expense-bot",
    "description": "报销助手",
    "url": "https://peer.example.com/a2a",
    "version": "1.2.0",
    "protocolVersion": "1.0.0",
    "capabilities": {"streaming": True, "pushNotifications": False},
    "skills": [
        {"id": "reimburse", "name": "报销", "description": "处理发票报销", "tags": ["finance"]}
    ],
    "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
    "security": [{"bearer": []}],
}

# 假证书 DER：指纹函数只 hash 字节，内容无需是真证书
_DER = b"\x30\x82\x01\x0a-fake-peer-cert"


class _Recorder:
    """MockTransport handler + 请求记录（照 test_dingtalk_api_mock 范式）。"""

    def __init__(self, payload=None, status: int = 200, text: str | None = None):
        self.requests: list[httpx.Request] = []
        self.payload = _CARD_PAYLOAD if payload is None else payload
        self.status = status
        self.text = text

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.payload)


def _mock_client(rec: _Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))


# ------------------------------ well-known 补全 ------------------------------ #
async def test_fetch_card_completes_well_known_path():
    rec = _Recorder()
    c = await card.fetch_card("https://peer.example.com", http_client=_mock_client(rec))
    assert isinstance(c, AgentCard)
    assert len(rec.requests) == 1
    assert rec.requests[0].url.path == "/.well-known/agent-card.json"
    assert rec.requests[0].method == "GET"


async def test_fetch_card_capability_endpoint_uses_token_scoped_well_known():
    # 能力地址 → 发现路径**带 token**。集成期实测修正：Ginno 入站全路径鉴权，
    # origin 级 well-known 无 token 一律 401，出站 client 会取不到 Card。
    # **不要改回 origin 级**（原断言 `"a2a" not in url.path` 是错的假设）。
    rec = _Recorder()
    tok = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    await card.fetch_card(
        f"https://peer.example.com:8790/a2a/{tok}#fp=" + "B" * 26,
        http_client=_mock_client(rec),
    )
    url = rec.requests[0].url
    assert url.path == f"/a2a/{tok}/.well-known/agent-card.json"
    # fragment 绝不进请求（#fp 是纯客户端本地校验用）
    assert not url.fragment


# ------------------------------ SSRF 表驱动 ------------------------------ #
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8790/a2a",
        "http://127.8.9.10/",
        "http://10.0.0.5/",
        "http://10.255.255.255/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.5:8790/",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://0.0.0.0/",
        "http://[::1]:8790/",
        "http://[fc00::1]/",
        "http://[fd12:3456::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:10.0.0.1]/",  # v4-mapped 绕过尝试
        "http://localhost:8790/",
        "http://foo.localhost/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///",  # 无 host
    ],
)
def test_guard_blocks_private_and_bad_scheme(endpoint):
    with pytest.raises(ValueError):
        card.guard_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://8.8.8.8/",
        "https://example.com/a2a",
        "http://172.32.0.1/",  # 172.16/12 之外
        "http://192.169.0.1/",  # 192.168/16 之外
        "http://[2001:db8::1]/",
        "https://peer.example.com:8790/.well-known/agent-card.json",
    ],
)
def test_guard_allows_public(endpoint):
    card.guard_endpoint(endpoint)  # 不抛即通过


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8790/", "http://10.0.0.5/", "http://localhost:8790/", "http://[::1]/"],
)
def test_guard_allow_private_whitelist(endpoint):
    # 用户显式配置的内网 peer 白名单：放行私网，但 scheme 检查不放松
    card.guard_endpoint(endpoint, allow_private=True)
    with pytest.raises(ValueError):
        card.guard_endpoint("ftp://10.0.0.5/", allow_private=True)


async def test_fetch_card_ssrf_blocks_before_any_request():
    rec = _Recorder()
    with pytest.raises(ValueError):
        await card.fetch_card("http://127.0.0.1:8790", http_client=_mock_client(rec))
    assert rec.requests == []  # 拦截发生在请求之前，零网络


# ------------------------------ 卡片校验 ------------------------------ #
def test_validate_card_empty_lists_all():
    missing = card.validate_card(AgentCard())
    assert missing == ["name", "url", "description", "version", "capabilities", "skills"]


def test_validate_card_name_url_are_hard_requirements():
    c = AgentCard(name="bot")
    missing = card.validate_card(c)
    assert "url" in missing and "name" not in missing
    c2 = AgentCard(url="https://x/a2a")
    assert "name" in card.validate_card(c2)


def test_validate_card_complete_returns_empty():
    c = AgentCard.from_dict(_CARD_PAYLOAD)
    assert card.validate_card(c) == []


def test_validate_card_whitespace_name_counts_missing():
    assert "name" in card.validate_card(AgentCard(name="   ", url="https://x"))


# ------------------------------ MockTransport 取卡 ------------------------------ #
async def test_fetch_card_parses_fields():
    rec = _Recorder()
    c = await card.fetch_card("https://peer.example.com", http_client=_mock_client(rec))
    assert c.name == "expense-bot"
    assert c.url == "https://peer.example.com/a2a"
    assert c.protocol_version == "1.0.0"
    assert c.supports_streaming is True
    assert c.skills[0].id == "reimburse"
    assert c.auth_schemes() == ["bearer"]


async def test_fetch_card_sends_auth_header():
    rec = _Recorder()
    await card.fetch_card(
        "https://peer.example.com", auth="Bearer tk-1", http_client=_mock_client(rec)
    )
    assert rec.requests[0].headers["Authorization"] == "Bearer tk-1"


@pytest.mark.parametrize("status", [404, 500, 401])
async def test_fetch_card_http_error_raises_a2a_error(status):
    rec = _Recorder(status=status)
    with pytest.raises(A2AError):
        await card.fetch_card("https://peer.example.com", http_client=_mock_client(rec))


async def test_fetch_card_bad_json_raises_a2a_error():
    rec = _Recorder(text="<html>not json</html>")
    with pytest.raises(A2AError):
        await card.fetch_card("https://peer.example.com", http_client=_mock_client(rec))


async def test_fetch_card_non_object_json_raises():
    rec = _Recorder(payload=["not", "an", "object"])
    with pytest.raises(A2AError):
        await card.fetch_card("https://peer.example.com", http_client=_mock_client(rec))


async def test_fetch_card_transport_error_wrapped():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(A2AError):
        await card.fetch_card(
            "https://peer.example.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )


# ------------------------------ 指纹校验 ------------------------------ #
async def test_fetch_card_fingerprint_ok(monkeypatch):
    calls = []

    def fake_peer(host, port, timeout_s):
        calls.append((host, port))
        return _DER

    monkeypatch.setattr(card, "_peer_cert_der", fake_peer)
    rec = _Recorder()
    c = await card.fetch_card(
        "https://peer.example.com:8790",
        verify_fp=fingerprint_of(_DER),
        http_client=_mock_client(rec),
    )
    assert c.name == "expense-bot"
    assert calls == [("peer.example.com", 8790)]
    assert len(rec.requests) == 1  # 校验通过后才发 GET


async def test_fetch_card_fingerprint_mismatch_aborts_before_request(monkeypatch):
    monkeypatch.setattr(card, "_peer_cert_der", lambda h, p, t: _DER)
    rec = _Recorder()
    with pytest.raises(A2AError, match="指纹不匹配"):
        await card.fetch_card(
            "https://peer.example.com",
            verify_fp=fingerprint_of(b"impostor-cert"),
            http_client=_mock_client(rec),
        )
    assert rec.requests == []  # 校验失败 → 一个字节都不发（防 MITM 先于凭据）


async def test_fetch_card_fingerprint_case_insensitive(monkeypatch):
    monkeypatch.setattr(card, "_peer_cert_der", lambda h, p, t: _DER)
    rec = _Recorder()
    await card.fetch_card(
        "https://peer.example.com",
        verify_fp=fingerprint_of(_DER).lower(),
        http_client=_mock_client(rec),
    )
    assert len(rec.requests) == 1


def test_ensure_fingerprint_mismatch_message():
    with pytest.raises(A2AError, match="冒充"):
        card.ensure_fingerprint(_DER, fingerprint_of(b"other"))


async def test_verify_endpoint_fingerprint_connection_error(monkeypatch):
    def refuse(host, port, timeout_s):
        raise OSError("connection refused")

    monkeypatch.setattr(card, "_peer_cert_der", refuse)
    with pytest.raises(A2AError, match="TLS"):
        await card.verify_endpoint_fingerprint("https://peer.example.com", "B" * 26)


# ------------------------------ 杂项 ------------------------------ #
def test_card_payload_roundtrip_through_json():
    # 确认测试夹具本身是合法 JSON（MockTransport 依赖可序列化）
    assert json.loads(json.dumps(_CARD_PAYLOAD)) == _CARD_PAYLOAD


# ── 指纹校验只适用 https（2026-11-16）──────────────────────────────────────


def test_verify_fingerprint_rejects_http_with_clear_message():
    """http 端点做指纹校验须 fail-loud 且报错可读，不再 SSL record layer failure。"""
    import pytest

    from ginno_runtime.a2a.card import verify_endpoint_fingerprint

    with pytest.raises(ValueError, match="仅适用于 https"):
        import asyncio

        asyncio.run(
            verify_endpoint_fingerprint("http://127.0.0.1:8790/a2a/X", "JB6T2NHRZXYMNJCAUEJJA6DVVD")
        )
