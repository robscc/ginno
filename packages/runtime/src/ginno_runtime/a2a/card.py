"""Agent Card 发现 / 获取 / 校验 + SSRF 防护（设计 §6.1、§10）。

Card 是**不可信输入**：name/description/skills 文本都会进模型上下文，是
提示注入载体——本模块只负责取回与结构校验，内容消毒由工具层负责
（delegate_agent seam 三层做法，设计 §7.3）。

TLS 指纹校验（verify_fp）走**独立 TLS 连接**取对端证书 DER 再比对：
不依赖 httpx 内部 stream API，MockTransport 可完整打桩；代价是多一次
握手，P0 可接受（SSE/连接复用优化留 P1）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

import httpx

from ginno_runtime import jsonx as json
from ginno_runtime._version import ginno_user_agent

from .address import fingerprint_of, well_known_url
from .errors import A2AError
from .models import AgentCard

# SSRF 黑名单：显式网段而非 ipaddress.is_private——后者随 Python 版本漂移
# （3.13 起语义有变），显式列表可表驱动测试、行为可预期。
_BLOCKED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "本机"路由（Linux 上 0.0.0.0 即 localhost）
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",  # 链路本地（含云元数据 169.254.169.254）
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",  # unspecified
        "::1/128",
        "fc00::/7",  # ULA
        "fe80::/10",  # 链路本地
    )
)


def guard_endpoint(endpoint: str, *, allow_private: bool = False) -> None:
    """SSRF 闸：拒绝私网/环回/链路本地 IP 与非 http(s) scheme，违规抛 ValueError。

    ``allow_private=True`` 仅用于用户**显式配置**的内网 peer 白名单（设计 §10）；
    scheme 检查不受其影响（ftp/file 永远不行）。

    已知边界（P0 取舍）：域名只拦 ``localhost`` / ``*.localhost`` 字面量，
    **不做同步 DNS 解析**——域名解析到私网的 rebinding 防护属接入向导的
    地址预检（P1），在 async 路径里做 getaddrinfo 会阻塞事件循环。
    """
    # 不补 scheme：无 scheme 的输入（含 "javascript:xxx" 伪 scheme）一律拒。
    # fetch_card 传入前已经 well_known_url 规整，必有合法 scheme。
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"SSRF 防护：仅允许 http/https，收到 {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise ValueError("SSRF 防护：地址缺少主机名")
    if allow_private:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError(f"SSRF 防护：拒绝环回域名 {host!r}") from None
        return
    # ::ffff:10.0.0.1 这类 v4-mapped 地址按真实 v4 判，否则绕过 v6 黑名单
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    for net in (_BLOCKED_V4 if ip.version == 4 else _BLOCKED_V6):
        if ip in net:
            raise ValueError(f"SSRF 防护：拒绝私网/环回/链路本地地址 {host}")


def ensure_fingerprint(cert_der: bytes, expected_fp: str) -> None:
    """比对证书 DER 指纹与地址里的期望值，不匹配抛 A2AError（可能被冒充）。

    大小写不敏感：指纹是给人抄写/核对的，base32 大小写不携带信息。
    """
    actual = fingerprint_of(cert_der)
    if actual.upper() != (expected_fp or "").strip().upper():
        raise A2AError(
            "TLS 证书指纹不匹配：对端可能被冒充（中间人），已中止连接。"
            f"期望 {expected_fp}，实际 {actual}"
        )


def _peer_cert_der(host: str, port: int, timeout_s: float) -> bytes:
    """独立 TLS 连接取对端证书 DER（同步；调用方用 asyncio.to_thread 包）。

    pinning 场景对端多为自签证书，CA 校验必然失败——这里刻意 CERT_NONE，
    信任锚是地址里的指纹而非 CA（等效 SSH known_hosts，指纹经带外复制）。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise A2AError("对端未返回 TLS 证书，无法校验指纹")
    return der


async def verify_endpoint_fingerprint(
    endpoint: str, expected_fp: str, *, timeout_s: float = 10.0
) -> None:
    """对 endpoint 的 host:port 做指纹校验（client 首连 / fetch_card 共用）。

    endpoint 须已含 scheme（调用方都已规整）；无 scheme 直接拒。
    """
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"指纹校验需要合法 http/https endpoint，收到 {endpoint!r}")
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        der = await asyncio.to_thread(_peer_cert_der, host, port, timeout_s)
    except (OSError, ssl.SSLError) as e:
        raise A2AError(f"无法建立 TLS 连接校验证书指纹：{e}") from e
    ensure_fingerprint(der, expected_fp)


async def fetch_card(
    endpoint: str,
    *,
    auth: str | None = None,
    verify_fp: str | None = None,
    timeout_s: float = 20.0,
    http_client: httpx.AsyncClient | None = None,
    allow_private: bool = False,
) -> AgentCard:
    """取回并解析 Agent Card（设计 §6.1 第 1 步）。

    - ``endpoint``：能力地址 / 标准 endpoint / 裸域名均可，自动归到 well-known；
    - ``auth``：完整 Authorization 头值（如 ``"Bearer xxx"``），Card 也可能要鉴权；
    - ``verify_fp``：先校验 TLS 指纹**再发任何 HTTP 请求**（校验通过才发凭据）；
    - ``http_client``：注入测试 transport；注入时不拥有其生命周期（不 close）。

    失败统一抛 A2AError（网络/HTTP/JSON），SSRF 与非法输入抛 ValueError——
    工具层只需 catch 这两类。
    """
    url = well_known_url(endpoint)
    guard_endpoint(url, allow_private=allow_private)
    if verify_fp:
        await verify_endpoint_fingerprint(url, verify_fp, timeout_s=timeout_s)
    headers = {"Accept": "application/json", "User-Agent": ginno_user_agent()}
    if auth:
        headers["Authorization"] = auth
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise A2AError(f"Agent Card 获取失败（网络错误）：{e}") from e
    finally:
        if owns:
            await client.aclose()
    if resp.status_code != 200:
        raise A2AError(
            f"Agent Card 获取失败：HTTP {resp.status_code}（对端可能未发布 Agent Card）"
        )
    try:
        data = json.loads(resp.text)
    except (ValueError, TypeError) as e:
        raise A2AError("Agent Card 响应不是合法 JSON") from e
    if not isinstance(data, dict):
        raise A2AError("Agent Card 响应不是 JSON 对象")
    return AgentCard.from_dict(data)


def validate_card(card: AgentCard) -> list[str]:
    """缺失关键字段清单（设计 §6.1 第 2 步「不完整」标注）。

    ``name`` / ``url`` 缺失 = 不可用（调用方应拒绝接入）；其余缺失只是
    不完整提示。返回空列表 = 完整。顺序固定，便于前端稳定渲染。
    """
    missing: list[str] = []
    if not card.name.strip():
        missing.append("name")
    if not card.url.strip():
        missing.append("url")
    if not card.description.strip():
        missing.append("description")
    if not card.version.strip():
        missing.append("version")
    if not card.capabilities:
        missing.append("capabilities")
    if not card.skills:
        missing.append("skills")
    return missing
