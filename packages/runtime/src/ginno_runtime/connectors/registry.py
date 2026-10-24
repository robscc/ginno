"""Connector registry (connector-module-design.md §3-4).

The single source of truth for「连没连、怎么装、怎么配」across every external
capability. Connectors report status transitions here; the registry debounces,
aggregates (sidebar status dot) and broadcasts ``connector_status_changed`` to
the UI. Config lives in ``settings.json`` under ``connectors.<id>``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ginno_runtime import jsonx as json

from .. import paths

log = logging.getLogger("ginno.connectors")

# 状态机 §4: not_installed → installing → connected ⇄ disconnected → error/disabled
STATUS_NOT_INSTALLED = "not_installed"
STATUS_INSTALLING = "installing"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
# mod 的 runner 正在重启(设计 §3.3:不装扮成 installing,单独一个状态)。
STATUS_RESTARTING = "restarting"

# 聚合状态点的优先级:越靠前越“响”。restarting 归入 warn。
_AGGREGATE_ORDER = [STATUS_ERROR, STATUS_DISCONNECTED, STATUS_RESTARTING,
                    STATUS_INSTALLING, STATUS_NOT_INSTALLED, STATUS_CONNECTED,
                    STATUS_DISABLED]
_DEBOUNCE_S = 3.0  # 断连 3s 内重连不闪黄(设计 §4)
_MISSING = object()  # configSummary:区分「存了默认值」和「没存」


@dataclass
class ConnectorState:
    status: str = STATUS_NOT_INSTALLED
    status_detail: str = ""
    version: str | None = None
    capabilities: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    # debounce bookkeeping: a disconnect reported within _DEBOUNCE_S of the
    # last transition is held pending; a reconnect cancels it, a read after
    # the window applies it (设计 §4:断连 3s 内重连不闪黄).
    _pending_status: str | None = None
    _pending_at: float = 0.0
    _last_change: float = 0.0


class Connector:
    """Base connector: identity + config plumbing. Subclasses own their
    connection lifecycle and call ``registry.report`` on transitions."""

    id: str = ""
    name: str = ""
    description: str = ""
    icon: str = "plug"           # frontend lucide icon name
    order: int = 100
    # 连接器类型(设计 §3.1):内置模块 / 由 mod 安装动态实例化。
    kind: str = "builtin"
    # 动态实例:随安装/卸载 register/unregister(mod 适配器)。动态 id 不能
    # 在未注册时回落到 enabled=True(设计 §8-1 的已知坑)。
    dynamic: bool = False
    # Tools whose names start with this prefix are provided by this connector
    # (设计 §8 per-agent 开关的判定依据)。双轨连接器声明同一前缀:浏览器能力
    # 只有在所有提供方都被该 agent 拒绝时才算关掉。None = 不提供工具。
    tool_prefix: str | None = None

    def default_config(self) -> dict:
        return {"enabled": True}

    def config_schema(self) -> dict:
        return {"type": "object", "properties": {
            "enabled": {"type": "boolean", "title": "Enabled"}},
        }

    # ---- 存储层(默认 settings.connectors.<id>)---------------------------------
    # 子类(mod 适配器)覆盖这两个方法即可把存储改到别处——上层 registry 只
    # 关心「取覆盖值 / 写覆盖值」,不关心落在哪个 settings 块。

    def read_stored_config(self) -> dict:
        """用户覆盖值(不含默认值),来自 settings.connectors.<id>。"""
        try:
            settings = json.loads(paths.settings_path().read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return {}
        stored = (settings.get("connectors") or {}).get(self.id) or {}
        return dict(stored) if isinstance(stored, dict) else {}

    def write_stored_config(self, cfg: dict) -> None:
        """合并写入。与旧 write_config 语义一致:默认值 ∪ 已存 ∪ 本次非 None。

        这样 settings 里的 ``connectors.<id>`` 始终是「完整生效值」,和改造前
        完全一样(内置连接器行为不变)。
        """
        stored = self.read_stored_config()
        base = dict(self.default_config())
        base.update(stored)
        base.update({k: v for k, v in (cfg or {}).items() if v is not None})
        settings_path = paths.settings_path()
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            settings = {}
        conns = settings.get("connectors") or {}
        conns[self.id] = base
        settings["connectors"] = conns
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2))

    def enabled_from_config(self, config: dict) -> bool:
        """列表里的 enabled。默认看合并后配置;mod 适配器改看 settings item。"""
        return bool(config.get("enabled", True))

    def install_steps(self) -> list[dict]:
        """Steps the module's install wizard renders (设计 §2.3)."""
        return []

    def detail_view(self) -> dict:
        """详情抽屉的声明式外观(设计 §3.5)。

        默认内置连接器:概览 / 设置(存在 ``enabled`` 之外的可配置项时)/
        活动。纯开关连接器不需要一个空设置页。
        """
        props = (self.config_schema() or {}).get("properties") or {}
        has_settings = any(k != "enabled" for k in props)
        tabs = [{"id": "overview", "kind": "overview", "title": "Overview"}]
        # 有安装步骤的连接器保留向导页签(否则声明了 detailView 后,前端不会
        # 再走 installSteps 兜底,安装入口会消失)。
        if self.install_steps():
            tabs.append({"id": "install", "kind": "install", "title": "Install"})
        if has_settings:
            tabs.append({"id": "settings", "kind": "settings", "title": "Settings"})
        tabs.append({"id": "activity", "kind": "activity", "title": "Activity"})
        return {"tabs": tabs, "actions": []}

    async def apply_config(self, cfg: dict) -> dict | None:
        """React to a config change (subclasses override as needed)."""


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}
        self._states: dict[str, ConnectorState] = {}
        self._listeners: list[Callable[[str, dict], None]] = []
        self._lock = threading.Lock()

    # ---- registration ------------------------------------------------------

    def register(self, conn: Connector) -> None:
        with self._lock:
            self._connectors[conn.id] = conn
            self._states.setdefault(conn.id, ConnectorState())
            # 动态实例(mod)不落默认值:它们的“启用”在 settings.mods.items,
            # 自动写 settings.connectors 会制造一份永远过期的影子配置。
            if not conn.dynamic and not self.read_config(conn.id):
                self.write_config(conn.id, conn.default_config())

    def unregister(self, cid: str) -> bool:
        """Remove a dynamically registered connector (mod uninstall).

        Also drops its state so a later re-register starts clean. Stored
        config is owned by the caller: a mod's config lives in settings.mods
        and is removed by the uninstall path, never here.
        """
        with self._lock:
            existed = self._connectors.pop(cid, None) is not None
            self._states.pop(cid, None)
            return existed

    def get(self, cid: str) -> Connector | None:
        return self._connectors.get(cid)

    def all(self) -> list[Connector]:
        return sorted(self._connectors.values(), key=lambda c: (c.order, c.id))

    # ---- status --------------------------------------------------------------

    def report(self, cid: str, status: str, detail: str = "", **kw: Any) -> None:
        """A connector reports a status transition (debounced, 设计 §4).

        A disconnect landing within _DEBOUNCE_S of the previous transition is
        held pending — a reconnect in that window cancels it (no UI flicker);
        a status read after the window applies it.
        """
        import time as _time

        with self._lock:
            st = self._states.setdefault(cid, ConnectorState())
            now = _time.time()
            for k in ("version", "capabilities", "extra"):
                if k in kw:
                    setattr(st, k, kw[k])
            if status == st.status:
                st._pending_status = None  # reconnect within window: cancel
                st.status_detail = detail
                return
            if (status == STATUS_DISCONNECTED
                    and st.status == STATUS_CONNECTED
                    and not kw.get("force")
                    and st._last_change
                    and now - st._last_change < _DEBOUNCE_S):
                st._pending_status = STATUS_DISCONNECTED
                st._pending_at = now
                return  # hold `connected` until the window passes
            st.status = status
            st.status_detail = detail
            st._last_change = now
            st._pending_status = None
            snapshot = self._snapshot_locked(cid)
        self._notify(snapshot)

    def report_meta(self, cid: str, **kw: Any) -> None:
        """Update version/capabilities/extra without a status transition."""
        with self._lock:
            st = self._states.setdefault(cid, ConnectorState())
            for k in ("version", "capabilities", "extra"):
                if k in kw:
                    setattr(st, k, kw[k])
            snapshot = self._snapshot_locked(cid)
        self._notify(snapshot)

    def status_of(self, cid: str) -> dict:
        import time as _time

        with self._lock:
            st = self._states.get(cid)
            if st and st._pending_status and _time.time() - st._pending_at >= _DEBOUNCE_S:
                st.status = st._pending_status
                st._pending_status = None
                st._last_change = _time.time()
            return self._snapshot_locked(cid)

    def aggregate_dot(self) -> str:
        """Sidebar 聚合状态点 (§2.1): '' none / 'warn' / 'error'.

        Connectors whose extra["idle"] is true (按需启动的 B 轨实例未运行
        属正常态) don't count towards the dot."""
        with self._lock:
            active = [
                s.status for s in self._states.values()
                if s.status != STATUS_DISABLED and not (s.extra or {}).get("idle")
            ]
        if STATUS_ERROR in active:
            return "error"
        if (STATUS_DISCONNECTED in active or STATUS_INSTALLING in active
                or STATUS_RESTARTING in active):
            return "warn"
        return ""

    def tool_denied(self, tool_name: str, denied_ids) -> bool:
        """Per-agent connector denial (设计 §8): True = 该 agent 不可用此工具。

        A tool is provided by every connector whose ``tool_prefix`` matches;
        it is denied only when ALL providers are in ``denied_ids`` — the
        dual-track browser (extension + profile) stays usable while either
        track is allowed. Tools no connector declares are never denied here.
        """
        with self._lock:
            providers = [c.id for c in self._connectors.values()
                         if c.tool_prefix and tool_name.startswith(c.tool_prefix)]
        if not providers:
            return False
        denied = set(denied_ids or [])
        return all(pid in denied for pid in providers)

    # ---- config (defaults ∪ connector storage) ---------------------------------

    def read_config(self, cid: str) -> dict:
        """defaults ∪ 连接器自己管理的存储(内置 = settings.connectors.<id>)。

        注册闸门(设计 §8-1):未注册 id 绝不能回落 enabled=True —— 动态实例
        (mod)卸载后残留的旧配置不能把已不存在的 mod 显示成“启用”。
        """
        conn = self._connectors.get(cid)
        if conn is None:
            return {"enabled": False}
        defaults = conn.default_config()
        stored = conn.read_stored_config()
        merged = dict(defaults)
        merged.update(stored)
        return merged

    def write_config(self, cid: str, cfg: dict) -> None:
        conn = self._connectors.get(cid)
        if conn is None:
            # 未注册 id 没有存储归属:忽略(动态实例已注销)。
            return
        conn.write_stored_config(cfg)

    # ---- events -----------------------------------------------------------------

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        self._listeners.append(fn)

    def _config_summary(self, conn: Connector) -> dict:
        defaults = conn.default_config() or {}
        try:
            stored = conn.read_stored_config() or {}
        except Exception:  # noqa: BLE001 — 摘要永远不能拖垮列表
            log.exception("connector stored config read failed: %s", conn.id)
            stored = {}
        # 只有存储里真实存在的键才算「已修改」;缺席 = 用默认值,不算改过。
        modified_keys = sorted(
            k for k, v in stored.items() if defaults.get(k, _MISSING) != v
        )
        return {
            "count": len(defaults),
            "modified": len(modified_keys),
            "modifiedKeys": modified_keys,
        }

    def _snapshot_locked(self, cid: str) -> dict:
        conn = self._connectors.get(cid)
        st = self._states.get(cid) or ConnectorState()
        merged = self.read_config(cid)
        try:
            detail_view = conn.detail_view() if conn else {"tabs": [], "actions": []}
        except Exception:  # noqa: BLE001 — 视图声明失败不影响状态读取
            log.exception("connector detail_view failed: %s", cid)
            detail_view = {"tabs": [], "actions": []}
        return {
            "id": cid,
            "name": conn.name if conn else cid,
            "icon": conn.icon if conn else "plug",
            "description": conn.description if conn else "",
            "kind": conn.kind if conn else "builtin",
            "status": st.status,
            "statusDetail": st.status_detail,
            "version": st.version,
            "capabilities": st.capabilities,
            "enabled": (conn.enabled_from_config(merged) if conn else False),
            "extra": st.extra,
            # 列表页据此渲染「安装指引」入口(设计 §2.3);详情接口会再覆盖一次
            "installSteps": conn.install_steps() if conn else [],
            "detailView": detail_view,
            "configSummary": (self._config_summary(conn) if conn
                              else {"count": 0, "modified": 0, "modifiedKeys": []}),
        }

    def _notify(self, snapshot: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn("connector_status_changed", snapshot)
            except Exception:  # noqa: BLE001 — listeners must not break us
                log.exception("connector listener failed")

    def list_payload(self) -> dict:
        with self._lock:
            ids = [c.id for c in self.all()]
        return {"connectors": [self.status_of(i) for i in ids],
                "aggregateDot": self.aggregate_dot()}


# ---- module singleton ------------------------------------------------------------

_registry: ConnectorRegistry | None = None


def registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry()
    return _registry
