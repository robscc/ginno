"""ginno node —— 无 GUI 机器的 headless A2A 节点（N1）。

设计：docs/design/ginno-node-headless-design.md（§2.1 进程模型 / §2.2 凭据落点）
+ docs/design/a2a-connector-design.md §6.2（能力地址）/ §11.3（BYOM 吸收）。

形态：**push 模式的标准 A2A server**——node 只监听，不主动向桌面端建立任何
连接（反向 WS/pull 已被主设计 §11.3.2 否决）。进程内跑 uvicorn（复用
``a2a/inbound.ensure_inbound_listener``，已幂等），阻塞至 SIGTERM/SIGINT，
**不 fork、不 daemonize**——长跑交给 systemd/launchd 或 ``setsid + nohup``。

凭据落点（§2.2，不新造格式）：
- ``GINNO_HOME`` 默认 ``~/.ginno-node``，与桌面端 ``~/.ginno`` 隔离；
  ``--home`` > 环境变量 ``GINNO_HOME`` > 默认。
- 配置走现有 ``paths.py`` + settings 机制：生效值合并写入
  ``settings.json`` 的 ``connectors.a2a`` 枢纽块（与桌面端 A2AHubConnector
  同一存储位，键见设计 §12.A/§12.B：node_enabled / inbound_exec_root 等）。
- token 只存 SHA-256 hash（``a2a/auth.TokenStore``）；能力地址明文 token
  **只在首次签发时打印一次到 stdout**，此后的运行只回显掩码地址。
  注意：inbound.py 既有机制为让长期地址跨重启稳定（§11.3.4 long_term），
  会把明文落 ``a2a/inbound-token`` 独立 0600 文件——那是 inbound 层的既定
  行为，node 层不重复打印、不另造格式；tokens.json 本身永远只有 hash。
- 私钥/证书走 ``a2a/identity.py``（0600、原子写）；``--tls-cert/--tls-key``
  直通 uvicorn。未提供 TLS 时按 P0 明文起服，能力地址如实广告 http。

fail-closed 默认：trust=readonly / caps=["L0"]（与 connectors/a2a.py 一致）。
N1 不含执行层（入站真跑 agent 属 N2，共享模块 a2a/inbound_exec.py）；
trust/caps/exec_root 在此落盘，供 N2 执行侧强制消费。

不做（设计 §5）：反向 WS/pull、心跳、离线排队、L2 文件传取、systemd 模板。
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ginno_runtime import jsonx as json
from ginno_runtime import paths

from .._version import __version__
from . import address, identity
from .inbound import DEFAULT_PORT, InboundConfig, InboundHandle, ensure_inbound_listener

__all__ = [
    "NodeConfig",
    "build_parser",
    "cli_main",
    "ensure_node_listener",
    "parse_listen",
    "resolve_config",
    "resolve_home",
    "run_node",
]

#: 与桌面端 ~/.ginno 隔离的默认 home（设计补充 §2.2）。
DEFAULT_HOME_NAME = ".ginno-node"
DEFAULT_NAME = "Ginno Node"
#: root 边界默认值（主设计 §11.3.1）；越界强制属 N2 执行层。
DEFAULT_ROOT = "~/ginno-a2a-workspace"

_BIND_MODES = ("loopback", "lan", "custom")
_TRUSTS = ("readonly", "normal", "full")
_CAPS = ("L0", "L1", "L2", "L3")


@dataclass
class NodeConfig:
    """node 形态的生效配置（CLI > settings 存值 > 默认，resolve_config 产出）。"""

    home: Path
    name: str = DEFAULT_NAME
    bind_mode: str = "loopback"
    bind_interface: str = ""
    port: int = DEFAULT_PORT
    root: Path = field(default_factory=lambda: Path(DEFAULT_ROOT).expanduser())
    tls_cert: str | None = None
    tls_key: str | None = None
    trust: str = "readonly"
    caps: list[str] = field(default_factory=lambda: ["L0"])

    @property
    def scheme(self) -> str:
        """广告 scheme 与实际起服形态一致：有 TLS 才 https（P0 默认明文 http）。"""
        return "https" if self.tls_cert and self.tls_key else "http"


# --------------------------------------------------------------------------- #
# CLI 解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ginno node",
        description=(
            "Headless A2A node: run a standard A2A server on a machine "
            "without the Ginno desktop app (push mode; the main Ginno "
            "connects to this node). Blocks until SIGTERM/SIGINT."
        ),
    )
    p.add_argument(
        "--listen",
        default=None,
        metavar="HOST:PORT",
        help=(
            "bind address, e.g. 127.0.0.1:8790 / :8790 / [::1]:8790. "
            "IP literal implies --bind-mode custom; localhost/empty implies "
            "loopback. Port 0 = ephemeral (tests). Default: loopback :8790"
        ),
    )
    p.add_argument("--name", default=None, help=f"agent name (default: {DEFAULT_NAME!r})")
    p.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help=f"exec_root boundary for inbound tasks (default: {DEFAULT_ROOT})",
    )
    p.add_argument(
        "--home",
        default=None,
        metavar="DIR",
        help=f"GINNO_HOME override (default: $GINNO_HOME or ~/{DEFAULT_HOME_NAME})",
    )
    p.add_argument(
        "--bind-mode",
        choices=_BIND_MODES,
        default=None,
        help="loopback (default) / lan (private NIC, fail-closed) / custom (IP via --listen)",
    )
    p.add_argument("--tls-cert", default=None, metavar="P", help="TLS certificate PEM (with --tls-key)")
    p.add_argument("--tls-key", default=None, metavar="P", help="TLS private key PEM (with --tls-cert)")
    return p


def parse_listen(listen: str | None, bind_mode: str | None) -> tuple[str, str, int]:
    """``--listen`` × ``--bind-mode`` → (bind_mode, bind_interface, port)。

    规则（fail-closed，冲突一律报错不静默修正）：
    - 两者都缺省 → loopback :8790；
    - 只给 --listen：HOST 为 IP 字面量 → custom；空/localhost → loopback；
      其它主机名拒绝（不做 DNS 解析，避免绑到非预期地址）；
    - 显式 --bind-mode 优先：custom 的 IP 取自 --listen HOST；loopback 拒绝
      非环回 HOST；lan 的地址由网卡枚举决定，--listen 只给端口。
    wildcard/组播的拒绝在 inbound.resolve_bind 统一执行（任何档位都不返回
    wildcard），这里对 custom 提前校验 IP 字面量以便尽早报错。
    """
    host, port = "", DEFAULT_PORT
    if listen is not None:
        raw = listen.strip()
        if raw.startswith("["):  # IPv6 字面量：[::1]:8790
            close = raw.find("]")
            if close < 0 or not raw[close + 1 :].startswith(":"):
                raise ValueError(f"--listen 非法（IPv6 需 [HOST]:PORT 形式）: {listen!r}")
            host, port_s = raw[1:close], raw[close + 2 :]
        else:
            if ":" not in raw:
                raise ValueError(f"--listen 需要 HOST:PORT 形式: {listen!r}")
            host, port_s = raw.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError(f"--listen 端口非法: {port_s!r}") from None
        if not 0 <= port <= 65535:
            raise ValueError(f"--listen 端口越界（0–65535）: {port}")
        host = host.strip()

    if bind_mode is None:
        if listen is None or host in ("", "localhost"):
            return ("loopback", "", port)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(
                f"--listen 主机须为 IP 字面量或 localhost（不做 DNS 解析）: {host!r}"
            ) from None
        return ("custom", host, port)

    if bind_mode == "custom":
        if not host:
            raise ValueError("custom 档需要 --listen HOST:PORT 指定 IP 字面量")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"custom 档 HOST 须为 IP 字面量: {host!r}") from None
        return ("custom", host, port)
    if bind_mode == "loopback":
        if host not in ("", "localhost", "127.0.0.1"):
            raise ValueError(f"loopback 档与 --listen HOST={host!r} 冲突")
        return ("loopback", "", port)
    # lan：地址由 resolve_bind 枚举私网网卡决定（找不到 fail-closed）
    if host:
        raise ValueError("lan 档地址由网卡枚举决定，--listen 只给端口（HOST 留空，如 :8790）")
    return ("lan", "", port)


def resolve_home(cli_home: str | None) -> Path:
    """--home > $GINNO_HOME > ~/.ginno-node（与桌面端 ~/.ginno 隔离）。"""
    if cli_home:
        return Path(cli_home).expanduser().resolve()
    env = os.environ.get("GINNO_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / DEFAULT_HOME_NAME).resolve()


# --------------------------------------------------------------------------- #
# settings 落盘（现有 paths + settings 机制，不新造凭据格式）
# --------------------------------------------------------------------------- #


def _load_hub() -> dict:
    """读 settings.connectors.a2a 枢纽块（照 connectors/registry 的读法）。"""
    try:
        settings = json.loads(paths.settings_path().read_text() or "{}")
    except (OSError, ValueError):
        return {}
    hub = (settings.get("connectors") or {}).get("a2a") or {}
    return dict(hub) if isinstance(hub, dict) else {}


def _save_hub(effective: dict) -> None:
    """合并写入枢纽块（保留 audit_enabled 等其它键；照 registry 写法）。"""
    settings_path = paths.settings_path()
    try:
        settings = json.loads(settings_path.read_text() or "{}")
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    conns = settings.get("connectors") or {}
    hub = conns.get("a2a") or {}
    if not isinstance(hub, dict):
        hub = {}
    hub.update(effective)
    conns["a2a"] = hub
    settings["connectors"] = conns
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2))


def _normalize_policy(hub: dict) -> tuple[str, list[str]]:
    """trust/caps fail-closed 规整（照 connectors/a2a.add_peer 同款纪律）。"""
    trust = str(hub.get("trust") or "").strip()
    if trust not in _TRUSTS:
        trust = "readonly"
    caps_raw = hub.get("caps")
    caps = (
        [str(c) for c in caps_raw if str(c) in _CAPS]
        if isinstance(caps_raw, list) and caps_raw
        else []
    )
    return trust, caps or ["L0"]


def resolve_config(args: argparse.Namespace) -> NodeConfig:
    """CLI flag > settings 存值 > 默认；副作用：钉死 GINNO_HOME、初始化 layout、
    合并写回生效配置、创建 exec_root。非法输入抛 ValueError（cli_main 转退出码 2）。
    """
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError("--tls-cert 与 --tls-key 必须成对提供")
    for label, path in (("--tls-cert", args.tls_cert), ("--tls-key", args.tls_key)):
        if path and not Path(path).expanduser().is_file():
            raise ValueError(f"{label} 文件不存在: {path}")

    home = resolve_home(args.home)
    os.environ["GINNO_HOME"] = str(home)  # 此后一切 paths.* 都落在 node home
    paths.ensure_layout()
    hub = _load_hub()

    if args.listen is None and args.bind_mode is None:
        # 全缺省 → 恢复上次存值（跨重启稳定）
        mode = str(hub.get("bind_mode") or "loopback")
        if mode not in _BIND_MODES:
            mode = "loopback"
        iface = str(hub.get("bind_interface") or "")
        stored_port = hub.get("port")
        port = stored_port if isinstance(stored_port, int) and 0 <= stored_port <= 65535 else DEFAULT_PORT
    else:
        mode, iface, port = parse_listen(args.listen, args.bind_mode)
        if args.listen is None:
            stored_port = hub.get("port")
            if isinstance(stored_port, int) and 0 <= stored_port <= 65535:
                port = stored_port
            if mode == "custom" and not iface:
                iface = str(hub.get("bind_interface") or "")
                if not iface:
                    raise ValueError("custom 档需要 --listen HOST:PORT 指定 IP 字面量")

    name = args.name or str(hub.get("node_name") or "") or DEFAULT_NAME
    root_raw = args.root or str(hub.get("inbound_exec_root") or "") or DEFAULT_ROOT
    root = Path(root_raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    trust, caps = _normalize_policy(hub)

    cfg = NodeConfig(
        home=home,
        name=name,
        bind_mode=mode,
        bind_interface=iface,
        port=port,
        root=root,
        tls_cert=str(Path(args.tls_cert).expanduser()) if args.tls_cert else None,
        tls_key=str(Path(args.tls_key).expanduser()) if args.tls_key else None,
        trust=trust,
        caps=caps,
    )
    _save_hub(
        {
            "a2a_enabled": True,
            "inbound_enabled": True,
            "bind_mode": cfg.bind_mode,
            "bind_interface": cfg.bind_interface,
            # port=0 是临时端口（测试用），不落盘以免下次启动继续随机
            **({"port": cfg.port} if cfg.port else {}),
            "node_enabled": True,
            "node_name": cfg.name,
            "inbound_exec_root": str(cfg.root),
            "trust": cfg.trust,
            "caps": list(cfg.caps),
        }
    )
    return cfg


# --------------------------------------------------------------------------- #
# 起服 / 签发 / 阻塞
# --------------------------------------------------------------------------- #


def ensure_node_listener(cfg: NodeConfig) -> InboundHandle:
    """复用 inbound 的 ensure 语义：同一 (bind_ip, port, data_dir) 同一 handle。
    **不自动起服**——调用方 await handle.start()。"""
    config = InboundConfig(
        bind_mode=cfg.bind_mode,
        bind_interface=cfg.bind_interface,
        port=cfg.port,
        scheme=cfg.scheme,
        data_dir=paths.a2a_dir(),
        agent_name=cfg.name,
        agent_version=__version__,
    )
    return ensure_inbound_listener(config, ssl_certfile=cfg.tls_cert, ssl_keyfile=cfg.tls_key)


def _advertised_address(cfg: NodeConfig, handle: InboundHandle) -> str:
    """能力地址。指纹必须与**实际起服证书**一致，否则对端 pin 校验必挂：
    - 自带 --tls-cert/--tls-key：指纹取自所供证书（load_identity 顺带校验
      证书/私钥配对，不匹配 fail-loud）；不触发 inbound 的惰性 identity。
    - 无 TLS（P0 明文）：走 handle 的惰性自签 identity（与桌面端入站同款）。
    """
    port = handle.actual_port or handle.port
    if cfg.tls_cert and cfg.tls_key:
        fp = identity.load_identity(cfg.tls_cert, cfg.tls_key).fingerprint
    else:
        fp = handle.get_identity().fingerprint
    return address.build_address(f"{cfg.scheme}://{handle.bind_ip}:{port}", handle.token, fp)


def _print_banner(cfg: NodeConfig, handle: InboundHandle, *, first_issue: bool) -> None:
    # 逐行 flush：headless 下 stdout 常被 nohup/systemd 重定向成块缓冲，
    # 能力地址必须起服即可见（否则用户要等缓冲满/进程退出才拿得到地址）。
    port = handle.actual_port or handle.port
    print(f"ginno node: listening on {cfg.scheme}://{handle.bind_ip}:{port} ({cfg.bind_mode})", flush=True)
    print(f"home={cfg.home} exec_root={cfg.root} trust={cfg.trust} caps={','.join(cfg.caps)}", flush=True)
    addr = _advertised_address(cfg, handle)
    if first_issue:
        print("能力地址（首次签发；明文 token 仅显示这一次，请复制到主 Ginno 的 A2A 连接器）：", flush=True)
        print(addr, flush=True)
    else:
        fp = address.parse_address(addr).fingerprint
        print("能力地址已签发过，明文 token 不再回显（服务端只存 SHA-256 hash）。", flush=True)
        print(f"地址（token 掩码）：{cfg.scheme}://{handle.bind_ip}:{port}/a2a/****#fp={fp}", flush=True)
    print("停止：SIGTERM / SIGINT（Ctrl-C）", flush=True)


def _shutdown_event() -> asyncio.Event:
    """SIGTERM/SIGINT → event.set()。宿主进程拥有信号处理，listener 不抢
    （inbound 用 _NoSignalServer）；这里只装停止闸。"""
    ev = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, ev.set)
        except NotImplementedError:  # 非 Unix 平台兜底
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(ev.set))
    return ev


async def run_node(cfg: NodeConfig, *, stop_event: asyncio.Event | None = None) -> int:
    """起服 → 打印能力地址 → 阻塞至停止信号 → 优雅停。start/stop 均幂等。

    ``stop_event`` 供测试注入（已 set 的 event = 起服即收）；生产路径缺省，
    由 SIGTERM/SIGINT 触发。不 fork、不 daemonize。
    """
    data_dir = paths.a2a_dir()
    # ensure 内部会签发/复用 token——必须在 ensure 之前判定是否首次签发
    first_issue = not (data_dir / "inbound-token").exists()
    handle = ensure_node_listener(cfg)
    await handle.start()
    try:
        _print_banner(cfg, handle, first_issue=first_issue)
        if stop_event is None:
            stop_event = _shutdown_event()
        await stop_event.wait()
    finally:
        await handle.stop()
    return 0


def cli_main(argv: list[str]) -> int:
    """``ginno node`` 入口（照 checkpoint_segment.cli_main 范式，由 cli.main 分流）。"""
    args = build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ValueError as exc:
        print(f"ginno node: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_node(cfg))
    except KeyboardInterrupt:
        return 130
