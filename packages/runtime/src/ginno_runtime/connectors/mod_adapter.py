"""ModConnector — 每个已安装 mod 一个动态连接器实例(设计 §3.1)。

L1+L2 迁移的适配层:产品面(mod 出现在连接器列表),存储面不动——配置仍落在
``settings.mods.items.<name>.config``,启用态仍来自该 item 的 ``enabled``。
主进程侧的 broker 状态缓存由 ``mods.channel`` 提供;这里只做映射,不新增 IPC。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .registry import (
    STATUS_CONNECTED,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_INSTALLING,
    STATUS_RESTARTING,
    Connector,
)

log = logging.getLogger("ginno.connectors.mods")

# broker 生命周期 → 连接器状态(设计 §3.3)。无状态/未安装的 mod 一律 disabled
# (内置连接器才用 not_installed)。
_STATUS_MAP = {
    "loaded": STATUS_CONNECTED,
    "error": STATUS_ERROR,
    "restarting": STATUS_RESTARTING,
    "installing": STATUS_INSTALLING,
    "disabled": STATUS_DISABLED,
}


def _channel() -> Any:
    """mods channel singleton(不触发连接)。取不到就返回 None。"""
    try:
        from ..mods.channel import get_channel

        return get_channel()
    except Exception:  # noqa: BLE001 — 连接器列表不该被 mods 初始化拖垮
        return None


def _infer_property(value: Any) -> dict | None:
    """从默认值推断一个 JSON-Schema 子集。bool 必须先于 int 判断(bool 是 int)。"""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        spec: dict = {"type": "array"}
        if value:
            item = _infer_property(value[0])
            if item is not None and item.get("type") != "object":
                spec["items"] = item
        return spec
    # dict / None:不在表单里渲染(高级 JSON 逃生口)。
    return None


def _normalize_schema(reported: dict, defaults: dict, stored: dict) -> dict:
    """reported schema(若有)叠加默认值推断;缺失的类型/默认值补齐。

    支持透传的显式键:type/title/description/default/enum/items/
    x-ginno-group/x-ginno-order/x-ginno-secret/x-ginno-placeholder/
    x-ginno-min/x-ginno-max(以及任何其它 schema 键)。
    """
    values: dict[str, Any] = {}
    for src in (defaults, stored):
        for key, value in (src or {}).items():
            values.setdefault(key, value)

    props: dict[str, dict] = {}
    if isinstance(reported.get("properties"), dict):
        for key, raw in reported["properties"].items():
            prop = copy.deepcopy(raw) if isinstance(raw, dict) else {}
            if "type" not in prop:
                inferred = _infer_property(values.get(key))
                if inferred is not None:
                    prop.update(inferred)
            props[str(key)] = prop
    # reported schema 没覆盖到的默认值键:补推断(否则默认值在 UI 里不可见)。
    for key, value in values.items():
        if key in props:
            continue
        inferred = _infer_property(value)
        if inferred is not None:
            props[key] = inferred

    # 每个属性都带 default,渲染端可直接“恢复默认”。
    for key, prop in props.items():
        if "default" not in prop and key in values:
            prop["default"] = copy.deepcopy(values[key])

    schema = copy.deepcopy(reported) if isinstance(reported, dict) else {}
    schema.setdefault("type", "object")
    schema["properties"] = props
    return schema


class ModConnector(Connector):
    """One installed mod, presented as a connector. Dynamic: registered and
    unregistered by :func:`sync_mod_connectors` as the install set changes."""

    kind = "mod"
    dynamic = True
    icon = "puzzle"
    tool_prefix = None

    def __init__(self, row: dict, *, order: int = 200) -> None:
        self._row = dict(row or {})
        self.mod_name = str(self._row.get("name") or "")
        self.id = f"mod-{self.mod_name}"
        self.name = self.mod_name
        self.description = str(self._row.get("description") or "")
        self.version = str(self._row.get("version") or "")
        self.order = order

    # ---- row / identity -----------------------------------------------------

    def update_row(self, row: dict) -> None:
        """Refresh metadata from the latest merged mods row (id is stable)."""
        self._row = dict(row or {})
        self.description = str(self._row.get("description") or "")
        self.version = str(self._row.get("version") or "")

    def _defaults(self) -> dict:
        ch = _channel()
        fn = getattr(ch, "mod_defaults", None)
        if not callable(fn):
            return {}
        try:
            value = fn(self.mod_name)
        except Exception:  # noqa: BLE001 — 元数据缺失回落空默认值
            log.exception("mod defaults read failed: %s", self.mod_name)
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def _reported_schema(self) -> dict | None:
        ch = _channel()
        fn = getattr(ch, "mod_schema", None)
        if not callable(fn):
            return None
        try:
            value = fn(self.mod_name)
        except Exception:  # noqa: BLE001
            return None
        return value if isinstance(value, dict) and value else None

    # ---- config contract ----------------------------------------------------

    def default_config(self) -> dict:
        """runner 上报的 userConfig 默认值;旧 broker 不带 → {}。"""
        return self._defaults()

    def read_stored_config(self) -> dict:
        """settings.mods.items.<name>.config(用户覆盖值,不含默认值)。"""
        from ..mods import bridge_utils

        items = bridge_utils.load_mods_settings().get("items") or {}
        item = items.get(self.mod_name) or {}
        cfg = item.get("config")
        return dict(cfg) if isinstance(cfg, dict) else {}

    def write_stored_config(self, cfg: dict) -> None:
        """合并写回 settings.mods.items.<name>.config。

        合并后剔除「与默认值相等」的键:配置块保持为真正的覆盖值,重置某项
        (写回默认值)等于删掉覆盖。默认值未知(旧 broker)时原样保留。
        """
        from ..mods import bridge_utils

        patch: dict = {}
        # 连接器底座把 enabled 当 config 键下发;mod 的启用态属于 settings
        # item,不能混进 userConfig 配置块。
        if "enabled" in (cfg or {}):
            patch["enabled"] = bool(cfg["enabled"])
        stored = self.read_stored_config()
        merged = dict(stored)
        merged.update({
            k: v for k, v in (cfg or {}).items()
            if v is not None and k != "enabled"
        })
        defaults = self.default_config()
        if defaults:
            merged = {
                k: v for k, v in merged.items()
                if k not in defaults or defaults[k] != v
            }
        patch["config"] = merged
        bridge_utils.update_mod_item(self.mod_name, patch)

    def enabled_from_config(self, config: dict) -> bool:
        """mod 的启用态来自 settings item,不是 config(body)。"""
        return bool(self._row.get("enabled", True))

    def config_schema(self) -> dict:
        """reported schema 优先;否则由 defaults ∪ stored 推断。"""
        defaults = self.default_config()
        stored = self.read_stored_config()
        reported = self._reported_schema()
        return _normalize_schema(reported or {}, defaults, stored)

    def install_steps(self) -> list[dict]:
        # mod 由商店/目录安装,没有内置连接器那种向导;详情页用 overview。
        return []

    def detail_view(self) -> dict:
        props = (self.config_schema() or {}).get("properties") or {}
        tabs = [{"id": "overview", "kind": "overview", "title": "Overview"}]
        if any(k != "enabled" for k in props):
            tabs.append({"id": "settings", "kind": "settings", "title": "Settings"})
        # S3:权限后端未就绪,S4 补齐;先保留页签与 classicGrant 只读控制。
        tabs.append({"id": "permissions", "kind": "permissions", "title": "Permissions"})
        tabs.append({"id": "activity", "kind": "activity", "title": "Activity"})
        return {"tabs": tabs, "actions": []}

    def classic_grant(self) -> str:
        """现有 grants.classic(默认 ask)。写回走既有 PUT /api/mods。"""
        items = {}
        try:
            from ..mods import bridge_utils

            items = bridge_utils.load_mods_settings().get("items") or {}
        except Exception:  # noqa: BLE001
            return "ask"
        grants = (items.get(self.mod_name) or {}).get("grants") or {}
        value = str(grants.get("classic") or "ask")
        return value if value in ("allow", "ask", "deny") else "ask"

    async def apply_config(self, cfg: dict) -> dict:
        """写 settings → 下发 broker(apply-config)并等待 ack。从不抛异常。"""
        try:
            self.write_stored_config(cfg)
        except Exception as e:  # noqa: BLE001 — 写盘失败如实返回
            return {"ok": False, "reloaded": False,
                    "error": f"failed to save config: {type(e).__name__}: {e}"}

        ch = _channel()
        if ch is None or not getattr(ch, "is_connected", False):
            return {"ok": True, "reloaded": False,
                    "error": "mods runtime is not connected"}
        push = getattr(ch, "push_config", None)
        if not callable(push):
            return {"ok": True, "reloaded": False,
                    "error": "mods channel cannot apply config"}
        try:
            ack = await push()
        except Exception as e:  # noqa: BLE001 — 配置已保存,重载失败只上报
            return {"ok": True, "reloaded": False,
                    "error": f"{type(e).__name__}: {e}"}
        # 新 broker 的 apply-config 返回 ack:
        # {ok, reloaded, mods:[{name,status,error?}]}(设计 §3.4)。
        # 旧 broker 只返回 ready 形状 → 视为 RPC 成功、已重载。
        if not isinstance(ack, dict):
            return {"ok": True, "reloaded": True, "error": None}
        if ack.get("error"):
            return {"ok": False, "reloaded": False, "error": str(ack["error"])}
        mine = None
        for entry in ack.get("mods") or []:
            if isinstance(entry, dict) and entry.get("name") == self.mod_name:
                mine = entry
                break
        if mine is not None and str(mine.get("status") or "") == "error":
            return {"ok": False, "reloaded": bool(ack.get("reloaded")),
                    "error": str(mine.get("error") or "mod failed to reload")}
        return {
            "ok": True,
            "reloaded": bool(ack.get("reloaded", True)),
            "error": None,
        }


# ---- sync --------------------------------------------------------------------


def _status_for(row: dict) -> tuple[str, str]:
    """merged mods 行 → (连接器状态, 状态说明)。映射见设计 §3.3。"""
    if not row.get("enabled", True):
        return STATUS_DISABLED, "Disabled"
    status = str(row.get("status") or "")
    if not status:
        # 无 broker 状态:安装目录存在但没跑(或配置残留)。mod 侧统一 disabled。
        detail = "Not running" if row.get("installed") else "Not installed"
        return STATUS_DISABLED, detail
    mapped = _STATUS_MAP.get(status)
    if mapped is None:
        return STATUS_DISABLED, status
    detail = str(row.get("detail") or row.get("statusDetail") or "")
    if not detail:
        detail = {
            STATUS_CONNECTED: "Loaded",
            STATUS_ERROR: "Runner error",
            STATUS_RESTARTING: "Restarting runner",
            STATUS_INSTALLING: "Starting",
            STATUS_DISABLED: "Disabled",
        }.get(mapped, "")
    return mapped, detail


def sync_mod_connectors(reg: Any = None) -> list[str]:
    """Idempotent: mirror the installed mods set into the connector registry.

    - 新 mod → 注册 ``ModConnector``(动态实例);
    - 仍在 → 刷新行元数据并重新上报状态;
    - 消失 → ``unregister``(注册闸门保证旧 id 不会回落 enabled=True)。

    Returns the currently registered ``mod-*`` ids (useful for tests/logging).
    """
    from ..mods import bridge_utils

    if reg is None:
        from .registry import registry

        reg = registry()

    channel = _channel()
    try:
        rows = bridge_utils.merged_mod_list(channel)
    except Exception:  # noqa: BLE001 — 同步失败保留既有实例,不制造空洞
        log.exception("mod rows scan failed; keeping existing connector set")
        return [c.id for c in reg.all() if getattr(c, "kind", "") == "mod"]

    active: set[str] = set()
    for index, row in enumerate(rows):
        name = str(row.get("name") or "")
        if not name:
            continue
        cid = f"mod-{name}"
        active.add(cid)
        conn = reg.get(cid)
        if not isinstance(conn, ModConnector) or conn.mod_name != name:
            conn = ModConnector(row, order=200 + index)
            reg.register(conn)
        else:
            conn.update_row(row)
        status, detail = _status_for(row)
        reg.report(
            cid,
            status,
            detail,
            version=str(row.get("version") or ""),
            extra={
                "source": row.get("source"),
                "origin": row.get("origin"),
                "compat": row.get("compat") or {},
                "modName": name,
            },
            force=True,
        )

    # 消失的 mod:注销动态实例(按 kind 判定,不受内置连接器影响)。
    for conn in list(reg.all()):
        if getattr(conn, "kind", "") == "mod" and conn.id not in active:
            reg.unregister(conn.id)

    return sorted(active)


__all__ = ["ModConnector", "sync_mod_connectors"]