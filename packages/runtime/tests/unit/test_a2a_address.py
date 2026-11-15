"""能力地址解析/生成的纯离线单测（零网络，设计 §6.1/§6.2）。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from ginno_runtime.a2a.address import (
    CapabilityAddress,
    build_address,
    fingerprint_of,
    parse_address,
    well_known_url,
)

pytestmark = pytest.mark.unit

# 26 字符、全在 base32 字母表（A-Z2-7）内
TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
FP = "234567ABCDEFGHIJKLMNOPQRST"  # 6 + 20 = 26 字符


# ------------------------------ 往返 ------------------------------ #
def test_roundtrip_capability_address():
    addr = build_address("https://192.168.1.5:8790", TOKEN, FP)
    assert addr == f"https://192.168.1.5:8790/a2a/{TOKEN}#fp={FP}"
    parsed = parse_address(addr)
    assert parsed == CapabilityAddress(
        endpoint=f"https://192.168.1.5:8790/a2a/{TOKEN}",
        token=TOKEN,
        fingerprint=FP,
        one_time=False,
    )
    assert parsed.is_capability


def test_roundtrip_one_time():
    addr = build_address("https://h:8790", TOKEN, FP, one_time=True)
    assert addr.endswith("#fp=" + FP + "&ot=1")
    parsed = parse_address(addr)
    assert parsed.one_time is True
    assert parsed.fingerprint == FP


def test_roundtrip_without_fingerprint():
    addr = build_address("https://h:8790", TOKEN, "")
    assert "#" not in addr
    parsed = parse_address(addr)
    assert parsed.fingerprint == ""
    assert parsed.token == TOKEN


def test_build_strips_path_and_query_from_endpoint():
    # endpoint 语义是 origin：误带 path/query 也不该漏进地址
    addr = build_address("https://h:8790/a2a/old?x=1#frag", TOKEN, FP)
    assert addr == f"https://h:8790/a2a/{TOKEN}#fp={FP}"


# ------------------------------ 解析 ------------------------------ #
def test_parse_standard_endpoint_has_no_token():
    parsed = parse_address("https://api.example.com/a2a")
    assert parsed.endpoint == "https://api.example.com/a2a"
    assert parsed.token == ""
    assert parsed.fingerprint == ""
    assert not parsed.is_capability


def test_parse_bare_domain_adds_https():
    parsed = parse_address("example.com")
    assert parsed.endpoint == "https://example.com"
    assert parsed.token == ""


def test_fragment_never_lands_in_endpoint():
    # fragment 不随 HTTP 请求发送——endpoint 必须干净，否则 fp 会泄进 path
    parsed = parse_address(f"https://h:8790/a2a/{TOKEN}#fp={FP}")
    assert "#" not in parsed.endpoint
    assert FP not in parsed.endpoint
    assert parsed.endpoint == f"https://h:8790/a2a/{TOKEN}"


def test_parse_keeps_query_on_capability_endpoint():
    parsed = parse_address(f"https://h/a2a/{TOKEN}?v=2#fp={FP}")
    assert parsed.endpoint == f"https://h/a2a/{TOKEN}?v=2"


def test_token_case_preserved_as_credential():
    # token 是凭据：大小写宽容识别，但原样保留（归一化可能改变对端比对结果）
    lower = TOKEN.lower()
    parsed = parse_address(f"https://h/a2a/{lower}")
    assert parsed.token == lower


def test_parse_ipv6_host():
    parsed = parse_address(f"https://[2001:db8::1]:8790/a2a/{TOKEN}#fp={FP}")
    assert parsed.endpoint == f"https://[2001:db8::1]:8790/a2a/{TOKEN}"
    assert parsed.token == TOKEN


# ------------------------------ 指纹 ------------------------------ #
def test_fingerprint_truncated_to_26_base32_chars():
    fp = fingerprint_of(b"\x30\x82\x01\x0a-fake-cert")
    assert len(fp) == 26
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in fp)


def test_fingerprint_matches_manual_sha256_base32():
    der = b"another-cert-bytes"
    expected = base64.b32encode(hashlib.sha256(der).digest()).decode("ascii")[:26]
    assert fingerprint_of(der) == expected


def test_fingerprint_deterministic_and_sensitive():
    assert fingerprint_of(b"a") == fingerprint_of(b"a")
    assert fingerprint_of(b"a") != fingerprint_of(b"b")


# ------------------------------ well-known ------------------------------ #
def test_well_known_url_completion():
    assert well_known_url("example.com") == "https://example.com/.well-known/agent-card.json"
    assert well_known_url("https://h:8790") == "https://h:8790/.well-known/agent-card.json"


def test_well_known_url_idempotent_and_token_aware():
    wk = "https://h/.well-known/agent-card.json"
    assert well_known_url(wk) == wk
    # 标准 endpoint / 裸域名 → origin 级公开发现路径（A2A 规范）
    assert well_known_url("https://h") == wk
    assert well_known_url("https://h/some/path?x=1") == wk
    # 能力地址 → **token 路径下**的 well-known。
    # 集成期实测修正：Ginno 入站 fail-closed 全路径鉴权（_AuthGate 跑在路由前），
    # origin 级没 token 一律 401 → 出站 client 发现不了本机 Card，接入向导必挂。
    # 原实现归到 origin 级是基于「Card 是公开文档」的标准 A2A 假设，对本产品不成立。
    # **不要改回 origin 级**——那会让 Ginno↔Ginno 接入重新断掉。
    assert (
        well_known_url(f"https://h/a2a/{TOKEN}?x=1#fp={FP}")
        == f"https://h/a2a/{TOKEN}{wk[len('https://h'):]}"
    )
    # 幂等：对已是 token 级 well-known 的输入再规整一次不应重复叠加
    once = well_known_url(f"https://h/a2a/{TOKEN}")
    assert well_known_url(once) == once


# ------------------------------ 畸形输入 ------------------------------ #
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "https://",  # 无 host
        "ftp://example.com/a2a",  # 非 http(s)
        "file:///etc/passwd",
        "https://h/a2a/SHORT",  # token 截断
        "https://h/a2a/" + "A" * 25,  # 25 字符
        "https://h/a2a/" + "A" * 27,  # 27 字符
        "https://h/a2a/" + "1" * 26,  # base32 无 '1'
        "https://h/a2a/" + "A" * 25 + "!",  # 非法字符
        f"https://h/a2a/{TOKEN}#fp=SHORT",  # fp 截断
        f"https://h/a2a/{TOKEN}#fp=" + "B" * 25,
        f"https://h/a2a/{TOKEN}#fp=" + "0" * 26,  # base32 无 '0'
    ],
)
def test_parse_malformed_raises(bad):
    with pytest.raises(ValueError):
        parse_address(bad)


@pytest.mark.parametrize(
    "bad_token",
    ["", "SHORT", "A" * 25, "A" * 27, "1" * 26],
)
def test_build_malformed_token_raises(bad_token):
    with pytest.raises(ValueError):
        build_address("https://h", bad_token, FP)


def test_build_malformed_fingerprint_raises():
    with pytest.raises(ValueError):
        build_address("https://h", TOKEN, "0" * 26)
    with pytest.raises(ValueError):
        build_address("https://h", TOKEN, "B" * 25)


# ── 明文 http 不得附 TLS 指纹（2026-11-16 修 SSL record layer failure）──────


def test_build_address_http_drops_fingerprint():
    """http 明文无证书可钉，#fp 必须被丢弃（否则 client 必失败）。"""
    from ginno_runtime.a2a.address import build_address, parse_address

    tok = "Q3EQXAI3LK7GGT5KFSEYJWAXQB"
    fp = "JB6T2NHRZXYMNJCAUEJJA6DVVD"
    addr = build_address("http://127.0.0.1:8790", tok, fp)
    assert "#fp=" not in addr
    assert addr == f"http://127.0.0.1:8790/a2a/{tok}"
    assert parse_address(addr).fingerprint in ("", None)


def test_build_address_https_keeps_fingerprint():
    """https 仍须带指纹——安全语义绝不放宽。"""
    from ginno_runtime.a2a.address import build_address, parse_address

    tok = "Q3EQXAI3LK7GGT5KFSEYJWAXQB"
    fp = "JB6T2NHRZXYMNJCAUEJJA6DVVD"
    addr = build_address("https://192.168.1.5:8790", tok, fp)
    assert f"#fp={fp}" in addr
    assert parse_address(addr).fingerprint == fp


def test_build_address_http_one_time_flag_still_works():
    """丢 fp 不影响 ot=1 一次性标志。"""
    from ginno_runtime.a2a.address import build_address

    tok = "Q3EQXAI3LK7GGT5KFSEYJWAXQB"
    addr = build_address("http://h:1", tok, "JB6T2NHRZXYMNJCAUEJJA6DVVD", one_time=True)
    assert addr.endswith("#ot=1")
    assert "fp=" not in addr
