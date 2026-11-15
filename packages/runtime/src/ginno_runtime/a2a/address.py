"""能力地址（Capability URL）解析与生成 —— 地址即凭据（设计 §6.1/§6.2）。

形态：``https://host:port/a2a/<token>#fp=<base32指纹>``

- ``token``：26 字符 base32 ≈130bit 随机串，**就是凭据**，随 URL path 发送；
- ``#fp``：对端 TLS 证书指纹（SHA-256 → base32 → 前 26 字符，128bit 截断）。
  **URL fragment 不随 HTTP 请求发送**——纯客户端本地校验用：先比对指纹，
  通过才发 token，首次连接即防 MITM（设计 §6.2）；
- ``&ot=1``（fragment 内）：一次性地址标记，同样只在客户端本地生效。

本模块是纯解析/纯函数层，零网络 IO；TLS 指纹的实际校验在 card 模块。
token 与指纹按 base32 字母表（A-Z2-7）校验，大小写宽容、**原样保留**——
token 是凭据，擅自归一化大小写可能改变对端的比对结果。
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

#: RFC 8615 well-known 发现路径（设计 §3：Card 发现的标准入口）。
WELL_KNOWN_PATH = "/.well-known/agent-card.json"

_TOKEN_LEN = 26  # 26 × 5bit = 130bit（设计 §6.2）
_FP_LEN = 26  # SHA-256 base32 前 26 字符 ≈ 128bit 截断

_B32_26 = re.compile(r"^[A-Za-z2-7]{26}$")
_CAP_PATH = re.compile(r"^/a2a/([^/]+)/?$")


@dataclass
class CapabilityAddress:
    """解析结果。``endpoint`` 是消息端点：能力地址含 ``/a2a/<token>``，
    标准 endpoint / 域名则只有 origin（或原 path），**永远不含 fragment**。"""

    endpoint: str
    token: str = ""
    fingerprint: str = ""
    one_time: bool = False

    @property
    def is_capability(self) -> bool:
        """是否能力地址（带 token）。False = 标准 endpoint / 域名输入。"""
        return bool(self.token)


def _split(url: str) -> tuple[str, str, str, str, str]:
    """urlsplit + 裸域名补 https。scheme/host 合法性由调用方按需检查。"""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("地址为空")
    if "://" not in raw:
        # 裸域名补 https；但 "javascript:xxx" 这类伪 scheme 不能当域名补——
        # 保留原样，让下面的 scheme 检查拒绝
        if ":" not in raw.split("/", 1)[0]:
            raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 地址，收到 {parts.scheme!r}")
    if not parts.hostname:
        raise ValueError("地址缺少主机名")
    return parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment


def parse_address(url: str) -> CapabilityAddress:
    """解析能力地址 / 标准 endpoint / 裸域名（设计 §6.1 第 1 步的三种输入）。

    - ``/a2a/<token>`` 路径 → 能力地址：token 必须是 26 字符 base32，
      不合法立即报错（复制截断/手滑改字符要尽早暴露，而不是连上才 401）；
    - 无该路径 → 标准 endpoint，token/fingerprint 为空；
    - fragment 里的 ``fp=`` 一律校验格式（可能被篡改/截断），``ot=1`` 置一次性标志。
    """
    scheme, netloc, path, query, fragment = _split(url)
    origin = f"{scheme}://{netloc}"
    endpoint = origin
    token = ""
    m = _CAP_PATH.match(path)
    if m:
        token = m.group(1)
        if not _B32_26.match(token):
            raise ValueError("能力地址 token 非法：应为 26 字符 base32（可能被截断或改动）")
        endpoint = f"{origin}/a2a/{token}"
    elif path and path != "/":
        endpoint = origin + path
    if query and endpoint != origin:
        endpoint += "?" + query

    fingerprint = ""
    one_time = False
    if fragment:
        frag = dict(
            kv.split("=", 1) for kv in fragment.split("&") if "=" in kv
        )
        if "fp" in frag:
            fingerprint = frag["fp"]
            if not _B32_26.match(fingerprint):
                raise ValueError("证书指纹非法：应为 26 字符 base32（可能被截断或改动）")
        if frag.get("ot") == "1":
            one_time = True
    return CapabilityAddress(
        endpoint=endpoint, token=token, fingerprint=fingerprint, one_time=one_time
    )


def build_address(
    endpoint: str, token: str, fingerprint: str, *, one_time: bool = False
) -> str:
    """生成能力地址（入站侧用）。``endpoint`` 取 origin，path/query 一律丢弃。

    fingerprint 允许为空（标准模式，不带 fragment）；one_time=True 追加 ``&ot=1``。
    """
    scheme, netloc, _path, _query, _fragment = _split(endpoint)
    if not _B32_26.match(token or ""):
        raise ValueError("token 非法：应为 26 字符 base32")
    fp = (fingerprint or "").strip()
    if fp and not _B32_26.match(fp):
        raise ValueError("指纹非法：应为 26 字符 base32")
    addr = f"{scheme}://{netloc}/a2a/{token}"
    bits = []
    if fp:
        bits.append(f"fp={fp}")
    if one_time:
        bits.append("ot=1")
    if bits:
        addr += "#" + "&".join(bits)
    return addr


def fingerprint_of(cert_der: bytes) -> str:
    """证书 DER → SHA-256 → base32 → 前 26 字符（128bit 截断，设计 §6.2）。

    截断是刻意的：指纹要能口头核对/抄写，128bit 碰撞阻力对 pinning 足够；
    完整 SHA-256 仍在 TLS 握手内部起作用，这里只是带外比对锚点。
    """
    digest = hashlib.sha256(cert_der).digest()
    return base64.b32encode(digest).decode("ascii")[:_FP_LEN]


def well_known_url(endpoint: str) -> str:
    """任意输入 → Card 发现 URL（域名/无路径自动补 well-known，设计 §6.1）。

    **token 感知**（集成期实测修正）：
    - 标准 endpoint / 裸域名 → origin 级 ``/.well-known/agent-card.json``
      （A2A 规范的公开发现路径）。
    - **能力地址（``/a2a/<token>``）→ token 路径下的 well-known**。
      原实现一律归到 origin 级，理由是「Card 是公开文档」——那是标准 A2A 的
      假设，但 Ginno 入站 fail-closed **全路径鉴权**（server.py 的 _AuthGate
      跑在路由前），origin 级没 token 一律 401。实测：出站 client 用 origin 级
      well-known 发现本机入站 Card 直接 401，接入向导第 2 步必挂。
      能力地址本身已含凭据，发现路径带上它才自洽。

    query/fragment 对发现无意义，丢弃。幂等。
    """
    scheme, netloc, path, _query, _fragment = _split(endpoint)
    # 幂等：已是 well-known 路径（origin 级或 token 级）原样返回，不重复叠加、
    # 也不把 token 级退化回 origin 级（后者会重新触发 401）。
    if path.endswith(WELL_KNOWN_PATH):
        return urlunsplit((scheme, netloc, path, "", ""))
    m = _CAP_PATH.match(path)
    if m and _B32_26.match(m.group(1)):
        return urlunsplit(
            (scheme, netloc, f"/a2a/{m.group(1)}{WELL_KNOWN_PATH}", "", "")
        )
    return urlunsplit((scheme, netloc, WELL_KNOWN_PATH, "", ""))
