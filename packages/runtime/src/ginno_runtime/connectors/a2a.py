"""A2A 连接器（a2a-connector-design.md §5/§12）。

两层结构，照 mod_adapter 的动态实例范式：

- :class:`A2AHubConnector` —— 枢纽 ``id="a2a"``（builtin），入站与全局配置，
  存 ``settings.connectors.a2a``（基类存储）。P0 无 listener 副作用；
  ``apply_config`` 只刷新入站派生三字段（report_meta）。
- 管理 action 实现体（``preview_card`` / ``add_peer`` /
  ``reset_inbound_address``）：api/connectors.py 分发器的三个 a2a 分支
  委托到这里，契约见前端组件头注释（A2aPeerAddModal / A2aInboundCard）。
- :class:`A2APeerConnector` —— 每个出站 peer 一个动态实例
  （``id="a2a-<slug>"``，``dynamic=True``），配置存储覆盖为 PeerStore
  （``a2a/peers.json``），**不是** settings.connectors——避免一份永远过期
  的影子配置（mod_adapter 同款坑）。

信任边界按 agent 划分 → 每实例独立 ``tool_prefix``，复用
``registry.tool_denied`` 的 per-agent 工具闸（设计 §5）。

P0 不做连通探测：实例状态由 sync 报 disabled/disconnected，真实
connected/error 迁移留给工具层（client 模块并行开发中，此处不 import）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import paths
from ..a2a.peers import PeerRecord, PeerStore, is_masked_token
from .registry import (
    STATUS_DISABLED,
    STATUS_DISCONNECTED,
    Connector,
)

log = logging.getLogger("ginno.connectors.a2a")


def _slug_clean(slug: str) -> str:
    """tool_prefix 用的 slug 清洗：非字母数字 → 下划线（expense-bot →
    expense_bot），工具名形如 ``a2a_expense_bot_send_task``。"""
    return re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")


class A2AHubConnector(Connector):
    """枢纽：入站开关与全局配置（设计 §12.A P0 子集）。"""

    id = "a2a"
    name = "A2A · 外部 Agent 协作"
    description = ("Agent2Agent 协议：把任务委托给外部 agent，或让局域网内"
                   "其他 agent 派活给本机（docs/design/a2a-connector-design.md）")
    icon = "network"
    order = 90
    kind = "builtin"
    tool_prefix = None  # 枢纽自身不提供工具;工具挂在 peer 实例上

    def default_config(self) -> dict:
        return {
            "a2a_enabled": False,
            "inbound_enabled": False,
            "bind_mode": "loopback",
            "bind_interface": "",
            "port": 8790,
            "admin_enabled": False,
            "audit_enabled": True,
        }

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "a2a_enabled": {
                    "type": "boolean", "title": "Enable A2A",
                    "description": "Master switch for outbound delegation "
                                   "and inbound listening",
                },
                "inbound_enabled": {
                    "type": "boolean", "title": "Allow inbound agents",
                    "description": "Let other agents on the network send "
                                   "tasks to this Ginno (fail-closed)",
                },
                "bind_mode": {
                    "type": "string",
                    "enum": ["loopback", "lan", "custom"],
                    "title": "Bind scope",
                    "description": "loopback: this machine only; lan: local "
                                   "network; custom: bind_interface below",
                },
                "bind_interface": {
                    "type": "string", "title": "Bind interface",
                    "description": "Only used when bind scope is custom "
                                   "(e.g. 192.168.1.10)",
                },
                "port": {
                    "type": "integer", "title": "Inbound port",
                    "description": "JSON-RPC over HTTP+SSE listener port",
                },
                "admin_enabled": {
                    "type": "boolean", "title": "Read-only admin endpoint",
                    "description": "R0 observation API (default off, §11.3)",
                },
                "audit_enabled": {
                    "type": "boolean", "title": "Audit log",
                    "description": "Record inbound/outbound task audit trail",
                },
            },
        }

    async def apply_config(self, cfg: dict) -> None:
        """P0 无 listener 可重启；配置落盘由基类 write_stored_config 完成。

        配置变更（尤其 inbound_enabled 开关）后刷新入站派生三字段
        （best-effort）：前端 PATCH 后 onReload 重拉列表即可见新地址/降级态。
        仅当枢纽已注册在全局 registry 时上报——隔离测试用独立 registry
        实例，不该被这里污染。返回值保持 None（既有契约）。
        """
        try:
            from .registry import registry

            reg = registry()
            if reg.get(self.id) is not None:
                report_inbound_meta(reg, cfg=cfg)
        except Exception:  # noqa: BLE001 — 派生值刷新失败不影响配置保存
            log.exception("a2a inbound meta refresh failed")


class A2APeerConnector(Connector):
    """One outbound peer, presented as a dynamic connector instance.

    Registered/unregistered by :func:`sync_peer_connectors` as the peer set
    changes（照 ModConnector 范式）。"""

    kind = "a2a"
    dynamic = True
    icon = "bot"
    order = 200

    def __init__(self, peer: PeerRecord, *, store: PeerStore | None = None,
                 order: int = 200) -> None:
        self._peer = peer
        self._store = store if store is not None else PeerStore()
        self.slug = peer.slug
        self.id = peer.id  # a2a-<slug>(设计 §5;PeerRecord 派生)
        self.name = peer.name
        self.description = peer.endpoint
        self.tool_prefix = f"a2a_{_slug_clean(peer.slug)}_"
        self.order = order

    def update_peer(self, peer: PeerRecord) -> None:
        """Refresh metadata from the latest PeerStore record (id is stable)."""
        self._peer = peer
        self.name = peer.name
        self.description = peer.endpoint

    # ---- config contract（存储覆盖为 PeerStore,不是 settings.connectors）----

    def default_config(self) -> dict:
        return {
            "enabled": True,
            "endpoint": "",
            "peer_fingerprint": "",
            "auth_scheme": "none",
            "token": "",
            "trust": "readonly",
            "caps": ["L0"],
            "confirm_mode": "every",
            "allow_local_files": False,
            "task_timeout_s": 600,
            "max_concurrent": 2,
        }

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "title": "Enabled"},
                "endpoint": {
                    "type": "string", "title": "Capability URL",
                    "description": "A2A agent address, e.g. "
                                   "https://host:port/a2a#fp=...",
                },
                "peer_fingerprint": {
                    "type": "string", "title": "Peer fingerprint",
                    "description": "Pinned TLS fingerprint from the address "
                                   "#fp fragment (address-as-credential)",
                },
                "auth_scheme": {
                    "type": "string",
                    "enum": ["none", "bearer", "api_key", "oauth2"],
                    "title": "Auth scheme",
                },
                "token": {
                    "type": "string", "title": "Token / API key",
                    "format": "password",
                },
                "trust": {
                    "type": "string",
                    "enum": ["readonly", "normal", "full"],
                    "title": "Trust level",
                    "description": "Pinned before dispatch and re-checked at "
                                   "execution; full does NOT grant config "
                                   "write (§11.3)",
                },
                "caps": {
                    "type": "array", "items": {"type": "string"},
                    "title": "Capability level",
                    "description": "L0–L3 ladder, constrained by trust",
                },
                "confirm_mode": {
                    "type": "string",
                    "enum": ["every", "after_first", "always"],
                    "title": "Dispatch confirmation",
                },
                "allow_local_files": {
                    "type": "boolean", "title": "Allow local file access",
                    "description": "Let the peer read local files via "
                                   "artifacts (default off)",
                },
                "task_timeout_s": {
                    "type": "integer", "title": "Task timeout (seconds)",
                },
                "max_concurrent": {
                    "type": "integer", "title": "Max concurrent tasks",
                },
            },
        }

    def read_stored_config(self) -> dict:
        """PeerStore 当前值，token 掩码（SEC-01：GET 响应只见掩码）。"""
        peer = self._store.find(self.slug)
        return peer.to_public() if peer is not None else {}

    def write_stored_config(self, cfg: dict) -> None:
        """合并写回 PeerStore。掩码 token 由 PeerStore.update 忽略；
        peer 已不存在时静默 no-op（动态实例可能已被 sync 注销）。"""
        if self._store.find(self.slug) is None:
            return
        try:
            self._store.update(self.slug, cfg or {})
        except KeyError:
            pass

    def install_steps(self) -> list[dict]:
        # peer 由枢纽「接入外部 Agent」向导添加,实例本身无安装向导。
        return []

    def detail_view(self) -> dict:
        # 四页签照 mod_adapter 形状;overview 的 Card 摘要为 P0 占位,
        # Card 全文渲染随 card 模块落地（设计 §5）。
        return {
            "tabs": [
                {"id": "overview", "kind": "overview", "title": "Overview"},
                {"id": "settings", "kind": "settings", "title": "Settings"},
                {"id": "permissions", "kind": "permissions",
                 "title": "Permissions"},
                {"id": "activity", "kind": "activity", "title": "Activity"},
            ],
            "actions": [],
        }

    async def apply_config(self, cfg: dict) -> dict:
        """配置已由 write_stored_config 落盘；P0 无运行时可重载。"""
        return {"ok": True, "reloaded": False, "error": None}


# ---- sync --------------------------------------------------------------------


def sync_peer_connectors(reg: Any = None, *,
                         store: PeerStore | None = None) -> list[str]:
    """Idempotent: mirror the PeerStore set into the connector registry.

    - 新 peer → 注册 ``A2APeerConnector``（动态实例）；
    - 仍在 → 刷新元数据并重新上报状态；
    - 消失 → ``unregister``（注册闸门保证旧 id 不会回落 enabled=True）。

    Returns the currently registered ``a2a-*`` instance ids.
    """
    if reg is None:
        from .registry import registry

        reg = registry()
    if store is None:
        store = PeerStore()

    try:
        peers = store.list_all()
    except Exception:  # noqa: BLE001 — 同步失败保留既有实例,不制造空洞
        log.exception("a2a peer scan failed; keeping existing connector set")
        return [c.id for c in reg.all() if getattr(c, "kind", "") == "a2a"]

    active: set[str] = set()
    for index, peer in enumerate(peers):
        cid = peer.id
        active.add(cid)
        conn = reg.get(cid)
        if not isinstance(conn, A2APeerConnector) or conn.slug != peer.slug:
            conn = A2APeerConnector(peer, store=store, order=200 + index)
            reg.register(conn)
        else:
            conn.update_peer(peer)
        if not peer.enabled:
            status, detail = STATUS_DISABLED, "Disabled"
        else:
            # P0 骨架不做连通探测;connected/error 迁移由工具层 report。
            status, detail = STATUS_DISCONNECTED, "Not connected"
        reg.report(
            cid,
            status,
            detail,
            extra={
                "peerSlug": peer.slug,
                "endpoint": peer.endpoint,
                "trust": peer.trust,
                "source": "a2a",
            },
            force=True,
        )

    # 消失的 peer:注销动态实例(按 kind 判定,不受内置/枢纽影响)。
    for conn in list(reg.all()):
        if getattr(conn, "kind", "") == "a2a" and conn.id not in active:
            reg.unregister(conn.id)

    return sorted(active)


# ---- 枢纽入站派生值（设计 §12.A 只读/派生：地址明文 + 指纹 + 实际监听）--------

#: extra 三字段契约（A2aInboundCard.tsx 头注释）：inbound_enabled=false 时
#: 一律空串——前端显示「未就绪」降级态，绝不造假地址。
_EMPTY_INBOUND_META = {
    "inboundAddress": "",
    "inboundFingerprint": "",
    "listenAddress": "",
}


def _write_secret_file(path: Path, text: str) -> None:
    """0600 原子写（照 a2a/inbound._load_or_issue_token 同款纪律）。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _ensure_identity(data_dir: Path, bind_ip: str):
    """惰性 generate-or-load 入站 identity。文件名与 InboundHandle.get_identity
    完全一致（inbound-cert.pem / inbound-key.pem）——未来 listener 起服加载
    的是同一份，指纹不会漂移。**重置地址只换 token，绝不走这里重新生成。**"""
    from ..a2a import identity as a2a_identity

    cert_path = data_dir / "inbound-cert.pem"
    key_path = data_dir / "inbound-key.pem"
    if cert_path.exists() and key_path.exists():
        return a2a_identity.load_identity(cert_path, key_path)
    ident = a2a_identity.generate_identity(name="Ginno", san_ips=[bind_ip])
    a2a_identity.save_identity(ident, cert_path=cert_path, key_path=key_path)
    return ident


def inbound_meta(cfg: dict) -> dict:
    """计算 extra 三字段（纯派生，可能 raise ValueError——调用方兜底）。

    - inbound_enabled=false → 三字段空串（降级态，不生成 identity/token）；
    - listenAddress = resolve_bind 结果:port（§6.3 三档绑定，绝不 wildcard）；
    - inboundAddress = 能力地址明文（§12.C-2：要复制分发，刻意不打码），
      advertised scheme 固定 https（生产形态，同 InboundConfig.scheme 默认；
      P0 环回自测的明文 http 起服属 listener 层，不影响地址形态）。
    """
    from ..a2a import address as a2a_address
    from ..a2a.auth import TokenStore
    from ..a2a.inbound import DEFAULT_PORT, _load_or_issue_token, resolve_bind

    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg.get("inbound_enabled"):
        return dict(_EMPTY_INBOUND_META)
    bind_ip = resolve_bind(str(cfg.get("bind_mode") or "loopback"),
                           str(cfg.get("bind_interface") or ""))
    port = int(cfg.get("port") or DEFAULT_PORT)
    data_dir = paths.a2a_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    tokens = TokenStore(data_dir / "tokens.json")
    token = _load_or_issue_token(tokens, data_dir / "inbound-token")
    ident = _ensure_identity(data_dir, bind_ip)
    return {
        "inboundAddress": a2a_address.build_address(
            f"https://{bind_ip}:{port}", token, ident.fingerprint),
        "inboundFingerprint": ident.fingerprint,
        "listenAddress": f"{bind_ip}:{port}",
    }


def report_inbound_meta(reg: Any = None, cfg: dict | None = None) -> None:
    """经 registry.report_meta 上报三字段（无状态迁移，只刷 extra）。

    计算失败（如 lan 档找不到私网地址 fail-closed）降级为空字段——
    宁缺毋假，且不把异常拖进列表/详情接口。
    """
    from .registry import registry as _registry_fn

    if reg is None:
        reg = _registry_fn()
    if cfg is None:
        cfg = reg.read_config("a2a")
    try:
        meta = inbound_meta(cfg)
    except Exception:  # noqa: BLE001 — 派生值失败降级空态
        log.exception("a2a inbound meta computation failed; reporting empty")
        meta = dict(_EMPTY_INBOUND_META)
    reg.report_meta("a2a", extra=meta)


def ensure_a2a_hub(reg: Any = None) -> A2AHubConnector:
    """幂等确保枢纽在 registry 里 + peer 实例同步 + 入站派生值上报。

    builtin.ensure_builtin_connectors 尚未挂接枢纽注册（集成轮范围外），
    管理 action 先经这里兜底，保证 action 触发后列表/详情可见枢纽。
    """
    from .registry import registry as _registry_fn

    if reg is None:
        reg = _registry_fn()
    hub = reg.get("a2a")
    if not isinstance(hub, A2AHubConnector):
        hub = A2AHubConnector()
        reg.register(hub)
    sync_peer_connectors(reg)
    report_inbound_meta(reg)
    return hub


# ---- 管理 action（api/connectors.py 分发器三个分支的实现体）------------------
# 契约见 A2aPeerAddModal.tsx / A2aInboundCard.tsx 头注释。共同纪律：
# - 异常一律压成 {ok:false,error:<限长诊断>}，绝不 raise 进 HTTP 层；
# - token 绝不出现在响应/日志/异常文本——错误文本先过 _scrub 掩码；
# - 唯一例外是 reset 的 address 字段：地址即凭据，明文回给本机 UI 是
#   设计 §12.C-2 的刻意行为（复制分发），与「回显 token」是两回事。


def _scrub(text: str, *secrets: str) -> str:
    """错误诊断限长 + 凭据掩码（httpx 异常文本会带完整 URL，含 path token）。"""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "••••")
    return out[:300]


def _card_payload(card: Any) -> dict:
    """AgentCard → 前端预览 dict（A2A wire camelCase；与 validate_card 同口径）。

    只回解析后的结构字段，不回 raw——raw 是不可信对端的任意 JSON。
    """
    return {
        "name": card.name,
        "description": card.description,
        "url": card.url,
        "version": card.version,
        "protocolVersion": card.protocol_version,
        "provider": card.provider,
        "capabilities": card.capabilities,
        "skills": [
            {"id": s.id, "name": s.name, "description": s.description,
             "tags": s.tags, "examples": s.examples,
             "inputModes": s.input_modes, "outputModes": s.output_modes}
            for s in card.skills
        ],
        "securitySchemes": card.security_schemes,
        "security": card.security,
        "defaultInputModes": card.default_input_modes,
        "defaultOutputModes": card.default_output_modes,
        "documentationUrl": card.documentation_url,
    }


async def preview_card(url: str) -> dict:
    """action=a2a_preview_card {url} → {ok,card,missing,private_network}。

    SSRF 策略（设计 §10.4）：判据是「是否用户显式配置」——向导里粘贴地址
    是用户主动行为，内网 peer 是合法场景，故 guard 用 allow_private=True
    放行私网；但先以 allow_private=False 探测一次，私网/环回命中就在响应
    里带 private_network:true 供前端出信任提示。scheme 闸不放宽（非
    http/https 永远拒）。https 能力地址带 #fp 时先做 TLS 指纹校验再取
    Card（校验通过才发凭据）；http（P0 环回自测）无证书可校，跳过 pinning。
    """
    from ..a2a import address as a2a_address
    from ..a2a import card as a2a_card

    token = ""
    try:
        raw = (url or "").strip()
        if not raw:
            return {"ok": False, "error": "地址为空"}
        parsed = a2a_address.parse_address(raw)
        token = parsed.token
        private_network = False
        try:
            a2a_card.guard_endpoint(parsed.endpoint, allow_private=False)
        except ValueError:
            private_network = True
        verify_fp = ""
        if parsed.fingerprint and urlsplit(parsed.endpoint).scheme == "https":
            verify_fp = parsed.fingerprint
        card = await a2a_card.fetch_card(
            parsed.endpoint,
            verify_fp=verify_fp or None,
            timeout_s=20.0,
            allow_private=True,
        )
        return {
            "ok": True,
            "card": _card_payload(card),
            "missing": a2a_card.validate_card(card),
            "private_network": private_network,
        }
    except Exception as e:  # noqa: BLE001 — 预览失败走前端手动填表兜底
        return {"ok": False, "error": _scrub(f"{type(e).__name__}: {e}", token)}


def _derive_slug(name: str, endpoint: str, store: PeerStore) -> str:
    """slug 派生：name 优先（小写/连字符/去非法字符），纯中文名清洗后为空
    则回落 endpoint host；冲突加 -2/-3 后缀。"""
    def clean(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")

    base = clean(name)
    if not base:
        base = clean(urlsplit(endpoint).hostname or "")
    if not base:
        base = "peer"
    slug, n = base, 2
    while store.find(slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def add_peer(payload: dict) -> dict:
    """action=a2a_add_peer → {ok,id,slug}。

    fail-closed 默认值：trust=readonly / caps=["L0"] / confirm_mode=every，
    前端没传或传了非法值一律落到最严档。token 为空且 endpoint 是能力地址
    时从地址取（地址即凭据）；掩码 token 视为未填（SEC-01）。写完立即
    sync_peer_connectors 让动态实例出现。
    """
    from ..a2a import address as a2a_address

    payload = payload if isinstance(payload, dict) else {}
    token = str(payload.get("token") or "").strip()
    if is_masked_token(token):
        token = ""
    try:
        endpoint_raw = str(payload.get("endpoint") or "").strip()
        if not endpoint_raw:
            return {"ok": False, "error": "endpoint 必填"}
        parsed = a2a_address.parse_address(endpoint_raw)
        if not token and parsed.is_capability:
            token = parsed.token
        fingerprint = (str(payload.get("peer_fingerprint") or "").strip()
                       or parsed.fingerprint)
        name = str(payload.get("name") or "").strip()
        store = PeerStore()
        slug = _derive_slug(name, parsed.endpoint, store)
        trust = str(payload.get("trust") or "").strip()
        if trust not in ("readonly", "normal", "full"):
            trust = "readonly"
        confirm_mode = str(payload.get("confirm_mode") or "").strip()
        if confirm_mode not in ("every", "after_first", "always"):
            confirm_mode = "every"
        auth_scheme = str(payload.get("auth_scheme") or "").strip()
        if auth_scheme not in ("none", "bearer", "api_key", "oauth2"):
            auth_scheme = "none"
        caps_raw = payload.get("caps")
        caps = ([str(c) for c in caps_raw]
                if isinstance(caps_raw, list) and caps_raw else ["L0"])
        record = PeerRecord(
            slug=slug, name=name or slug, endpoint=parsed.endpoint,
            token=token, peer_fingerprint=fingerprint,
            auth_scheme=auth_scheme, trust=trust, caps=caps,
            confirm_mode=confirm_mode,
        )
        store.add(record)
        ensure_a2a_hub()  # 同步动态实例 + 枢纽派生值
        return {"ok": True, "id": record.id, "slug": record.slug}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _scrub(f"{type(e).__name__}: {e}", token)}


def reset_inbound_address() -> dict:
    """action=a2a_reset_inbound_address → {ok,address}。危险操作，审计必记。

    只轮换能力 token（旧的立即 revoke → 旧地址 401），**identity 不动**——
    指纹不变，已配对对端无需重新配对，只需重新分发新地址。审计走
    a2a.server.AuditLog（只落 token hash 前 8 位，明文不落盘）。
    """
    from ..a2a import address as a2a_address
    from ..a2a.auth import TokenStore
    from ..a2a.inbound import DEFAULT_PORT, resolve_bind
    from ..a2a.server import AuditLog

    old, new = "", ""
    try:
        ensure_a2a_hub()
        from .registry import registry as _registry_fn

        reg = _registry_fn()
        cfg = reg.read_config("a2a")
        data_dir = paths.a2a_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        bind_ip = resolve_bind(str(cfg.get("bind_mode") or "loopback"),
                               str(cfg.get("bind_interface") or ""))
        port = int(cfg.get("port") or DEFAULT_PORT)
        tokens = TokenStore(data_dir / "tokens.json")
        token_path = data_dir / "inbound-token"
        if token_path.exists():
            old = token_path.read_text(encoding="utf-8").strip()
        if old:
            tokens.revoke(old)  # 旧地址立即失效（设计 §13.1 用例 5）
        new = tokens.issue(kind="long_term", note="inbound capability address")
        _write_secret_file(token_path, new)
        ident = _ensure_identity(data_dir, bind_ip)  # load 优先 → 指纹不变
        address = a2a_address.build_address(
            f"https://{bind_ip}:{port}", new, ident.fingerprint)
        AuditLog(data_dir / "inbound-audit.jsonl").record(
            method="inbound_address_reset", token=new, outcome="ok")
        report_inbound_meta(reg, cfg=cfg)
        return {"ok": True, "address": address}
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "error": _scrub(f"{type(e).__name__}: {e}", old, new)}


__all__ = [
    "A2AHubConnector",
    "A2APeerConnector",
    "sync_peer_connectors",
    "ensure_a2a_hub",
    "inbound_meta",
    "report_inbound_meta",
    "preview_card",
    "add_peer",
    "reset_inbound_address",
]