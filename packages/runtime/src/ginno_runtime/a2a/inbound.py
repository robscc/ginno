"""A2A 入站 listener 生命周期（设计 §6.3 / §10.1 / §12.A）。

P0 范围：仅环回明文 HTTP 自测。**TLS 起服属 P1**——``ssl_certfile`` /
``ssl_keyfile`` 参数已预留并直通 uvicorn.Config（默认 None = 明文）；
能力地址默认仍按生产形态生成 ``https://``，环回自测显式传 scheme="http"。

绑定纪律（§6.3，绝不 0.0.0.0 无脑全绑）：
  loopback → 127.0.0.1（默认）；
  lan      → 枚举网卡取私网地址（多网卡机器 VPN/Docker 桥/热点全绑会意外
             暴露到非预期网络）；找不到私网地址**fail-closed 报错**，
             绝不回退 wildcard；
  custom   → 显式 IP 字面量，拒绝 wildcard/组播。

identity 惰性生成：首次 capability_address() / TLS 起服时才调
identity.generate_identity，落 a2a 目录（私钥 0600，save_identity 保证）。
能力 token 同样落独立 0600 文件——地址要跨重启稳定（long_term 语义，
§11.3.4）；TokenStore 本身仍只存哈希，tokens.json 泄露不能重放地址。

门控（a2a_inbound_enabled 默认 false）在连接器层；本模块只在被显式启用后
调用。start/stop 幂等；ensure_inbound_listener 对同一 (bind, port, data_dir)
返回同一 handle。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from ginno_runtime import paths

from . import address, identity
from .auth import TokenStore
from .server import A2ADeps, AuditLog, TokenBucketLimiter, build_a2a_app

__all__ = [
    "InboundConfig",
    "InboundHandle",
    "ensure_inbound_listener",
    "list_lan_addresses",
    "resolve_bind",
]

#: 设计 §12.A：入站独立端口，绝不复用 8787。
DEFAULT_PORT = 8790

_BIND_MODES = ("loopback", "lan", "custom")

#: 同一 (bind_ip, port, data_dir) 复用同一 handle（ensure 语义）。
_HANDLES: dict[tuple[str, int, str], "InboundHandle"] = {}


@dataclass
class InboundConfig:
    """入站 listener 配置（对应设计 §12.A 枢纽键的子集，P0 骨架）。"""

    bind_mode: str = "loopback"
    bind_interface: str = ""  # custom: IP 字面量；lan 的网卡选择 UI 属 P1
    port: int = DEFAULT_PORT
    scheme: str = "https"  # 能力地址 advertised scheme（P0 起服仍是明文）
    data_dir: Path | None = None  # 缺省 paths.a2a_dir()（调用时解析，测试可隔离）
    agent_name: str = "Ginno"
    agent_version: str = "0.0.0"
    rate_per_min: int = 30
    max_concurrent: int = 2


def list_lan_addresses() -> list[str]:
    """stdlib 尽力枚举本机网卡地址（不引 netifaces 依赖）。

    两个来源：默认路由源地址（UDP connect 技巧——只查路由表，**不发任何
    包**）+ 主机名解析。过滤环回 / 链路本地 / unspecified / 组播；默认路由
    地址排最前。P1 的「枚举让用户选」UI 直接消费本函数。
    """
    candidates: list[str] = []
    for family, probe in (
        (socket.AF_INET, "8.8.8.8"),
        (socket.AF_INET6, "2001:4860:4860::8888"),
    ):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 80))
                candidates.append(sock.getsockname()[0])
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP):
            candidates.append(info[4][0])
    except OSError:
        pass
    out: list[str] = []
    for raw in candidates:
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])  # 去 v6 zone id
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            continue
        text = str(ip)
        if text not in out:
            out.append(text)
    return out


def resolve_bind(bind_mode: str, bind_interface: str = "") -> str:
    """§6.3 三档绑定解析。**任何档位都不返回 wildcard 地址**。"""
    if bind_mode == "loopback":
        return "127.0.0.1"
    if bind_mode == "custom":
        raw = (bind_interface or "").strip()
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise ValueError(
                f"custom bind 需要 IP 字面量，收到 {bind_interface!r}"
            ) from None
        if ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"拒绝绑定 {raw}：wildcard/组播地址永远不允许")
        return str(ip)
    if bind_mode == "lan":
        addrs = [ipaddress.ip_address(t) for t in list_lan_addresses()]
        for ip in addrs:  # 优先 IPv4 私网
            if ip.version == 4 and ip.is_private:
                return str(ip)
        for ip in addrs:  # 其次任意私网（如 v6 ULA）
            if ip.is_private:
                return str(ip)
        raise ValueError("未找到私网 LAN 地址；拒绝回退 wildcard 绑定（fail-closed）")
    raise ValueError(f"未知 bind_mode: {bind_mode!r}（应为 loopback/lan/custom）")


def _load_or_issue_token(tokens: TokenStore, path: Path) -> str:
    """能力地址跨重启稳定 → 明文 token 落独立 0600 文件（与只存哈希的
    TokenStore 分离：tokens.json 泄露仍不能重放地址）。首次签发走
    TokenStore.issue，此后读文件复用。"""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = tokens.issue(kind="long_term", note="inbound capability address")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(tmp, 0o600)  # mkstemp 本就 0600，显式声明契约
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return token


class _NoSignalServer(uvicorn.Server):
    """宿主进程拥有信号处理（sidecar SIGTERM 遗言链路）——listener 不抢。"""

    def install_signal_handlers(self) -> None:  # noqa: D102 - 见类 docstring
        pass


class InboundHandle:
    """一个独立 uvicorn listener 的生命周期句柄。start/stop 幂等。"""

    def __init__(
        self,
        config: InboundConfig,
        *,
        bind_ip: str,
        deps: A2ADeps,
        token: str,
        data_dir: Path,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ):
        self.config = config
        self.bind_ip = bind_ip
        self.port = config.port
        self.actual_port: int | None = None  # port=0 时 start 后回填真实端口
        self._deps = deps
        self._token = token
        self._data_dir = data_dir
        self._ssl_certfile = ssl_certfile
        self._ssl_keyfile = ssl_keyfile
        self._identity: identity.Identity | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def token(self) -> str:
        return self._token

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def get_identity(self) -> identity.Identity:
        """惰性 generate-or-load。私钥 0600（save_identity 的原子写保证）。"""
        if self._identity is None:
            cert_path = self._data_dir / "inbound-cert.pem"
            key_path = self._data_dir / "inbound-key.pem"
            if cert_path.exists() and key_path.exists():
                self._identity = identity.load_identity(cert_path, key_path)
            else:
                ident = identity.generate_identity(
                    name=self.config.agent_name, san_ips=[self.bind_ip]
                )
                identity.save_identity(ident, cert_path=cert_path, key_path=key_path)
                self._identity = ident
        return self._identity

    def capability_address(self, *, scheme: str | None = None) -> str:
        """完整能力地址：``<scheme>://<bind>:<port>/a2a/<token>#fp=<指纹>``。

        scheme 默认 https（生产形态，§6.2）；P0 环回明文自测传 "http"。
        port=0 起服后用真实端口。首次调用触发 identity 惰性生成。
        """
        scheme = scheme or self.config.scheme
        port = self.actual_port or self.port
        ident = self.get_identity()
        return address.build_address(
            f"{scheme}://{self.bind_ip}:{port}", self._token, ident.fingerprint
        )

    async def start(self) -> None:
        """起独立 uvicorn listener（task 形态）。幂等：已在跑直接返回。"""
        if self.running:
            return
        app = build_a2a_app(self._deps)
        cfg = uvicorn.Config(
            app,
            host=self.bind_ip,
            port=self.port,
            log_level="warning",
            lifespan="off",
            ssl_certfile=self._ssl_certfile,
            ssl_keyfile=self._ssl_keyfile,
        )
        self._server = _NoSignalServer(cfg)
        self._task = asyncio.create_task(self._server.serve())
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 10.0
        while not self._server.started:
            if self._task.done():
                self._task = None
                self._server = None
                raise RuntimeError(
                    f"A2A 入站 listener 启动失败（端口 {self.port} 被占用？）"
                )
            if loop.time() > deadline:
                await self.stop()
                raise RuntimeError("A2A 入站 listener 启动超时")
            await asyncio.sleep(0.01)
        if self._server.servers:
            self.actual_port = self._server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """优雅停（should_exit → 等 serve 收尾）。幂等：没在跑直接返回。"""
        task, server = self._task, self._server
        self._task = None
        self._server = None
        self.actual_port = None
        if task is None:
            return
        if server is not None:
            server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        except BaseException:
            # server 早已异常退出等情形：stop 保持幂等且不抛。
            pass


def ensure_inbound_listener(
    config: InboundConfig | None = None,
    *,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> InboundHandle:
    """构建（或复用）listener 句柄。**不自动起服**——调用方 await start()。

    同一 (bind_ip, port, data_dir) 返回同一 handle（ensure 语义）。
    P0 不启用 TLS：ssl_certfile/ssl_keyfile 默认 None（明文环回自测），
    参数直通 uvicorn.Config，P1 传 identity 的证书/私钥路径即可起 HTTPS。
    """
    config = config or InboundConfig()
    if config.bind_mode not in _BIND_MODES:
        raise ValueError(
            f"未知 bind_mode: {config.bind_mode!r}（应为 loopback/lan/custom）"
        )
    bind_ip = resolve_bind(config.bind_mode, config.bind_interface)
    data_dir = Path(config.data_dir) if config.data_dir is not None else paths.a2a_dir()
    key = (bind_ip, config.port, str(data_dir))
    existing = _HANDLES.get(key)
    if existing is not None:
        return existing
    data_dir.mkdir(parents=True, exist_ok=True)
    tokens = TokenStore(data_dir / "tokens.json")
    token = _load_or_issue_token(tokens, data_dir / "inbound-token")
    deps = A2ADeps(
        tokens=tokens,
        audit=AuditLog(data_dir / "inbound-audit.jsonl"),
        limiter=TokenBucketLimiter(config.rate_per_min),
        max_concurrent=config.max_concurrent,
        agent_name=config.agent_name,
        agent_version=config.agent_version,
    )
    handle = InboundHandle(
        config,
        bind_ip=bind_ip,
        deps=deps,
        token=token,
        data_dir=data_dir,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    _HANDLES[key] = handle
    return handle
