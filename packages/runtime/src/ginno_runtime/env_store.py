"""env 块的敏感分类与每键 CAS（compare-and-swap）存储语义。

settings.json 的 ``env`` 块供 bash 工具注入子进程环境（env_inject.py），
历史上只能通过整文档 PUT /api/settings 修改——两键并发保存互踩，且
GET /api/settings 对认证连接串（``postgres://user:pass@host``，键名不含
SECRET/TOKEN）明文回传。

本模块提供：

- **敏感判定**：键名模式（TOKEN/SECRET/…）或值形态（带 userinfo 的 URL
  = 认证连接串）命中即为敏感。敏感值经专用 API 呈现时**只回元数据**
  （set / kind / scheme / host / has_credentials），绝不回值。
- **每键版本 token**：``settings.env_versions.<key>.token``——每次写入该
  键时重新生成的随机 opaque 串（``secrets.token_hex``）。**不是值的
  hash**：低熵秘密（密码、短 token）的裸 SHA256 可被字典攻击还原，
  随机 token 与值内容无关，泄漏零信息。
- **CAS 应用**：:func:`apply_updates` 在 settings_store 的同一把写锁内
  做「读 → 全量 CAS 校验 → 全事务应用 → 原子写」。任一键版本冲突 →
  整个请求拒绝（409），不同键之间合并语义由「校验全部通过才落盘」
  保证；同一请求内同键重复出现 → 直接 400（全事务拒绝）。
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from . import settings_store

log = logging.getLogger(__name__)

# 键名级敏感模式。PWD 一并纳入：宁过度掩码（当前目录值泄漏面小，但若
# 用户真放秘密进 PWD 键也无法区分）。HTTP_PROXY 等代理键不命中（普通值）。
_SECRET_KEY_RE = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE_?KEY|"
    r"DSN|CONN(ECTIO)?N)",
    re.IGNORECASE,
)
# 值级：带 userinfo 的 URL（scheme://user[:pass]@host...）——认证连接串。
_URL_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^\s/@]+:[^\s/@]*@")

ENV_KEY = "env"
VERSIONS_KEY = "env_versions"

# env 值的合法类型（与 env_inject.load_extra_env 的消费面一致）。
_ALLOWED_VALUE_TYPES = (str, int, float, bool)


class EnvVersionConflict(Exception):
    """CAS 版本不匹配；全部更新被拒绝（全事务）。"""

    def __init__(self, conflicts: list[dict]) -> None:
        super().__init__("env version conflict")
        self.conflicts = conflicts


class EnvUpdateRejected(Exception):
    """请求本身不合法（重复键 / 坏键名 / 坏值类型 / 结构错误）→ 400。"""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def key_is_sensitive(name: str) -> bool:
    return bool(_SECRET_KEY_RE.search(name or ""))


def value_is_sensitive(value: Any) -> bool:
    """值形态判定：带 userinfo 的 URL 视为敏感（认证连接串）。"""
    if not isinstance(value, str) or not value:
        return False
    return bool(_URL_USERINFO_RE.match(value))


def entry_is_sensitive(key: str, value: Any) -> bool:
    return key_is_sensitive(key) or value_is_sensitive(value)


def _entry_kind(key: str, value: Any) -> str:
    if value_is_sensitive(value):
        return "url-with-auth"
    if key_is_sensitive(key):
        return "secret-name"
    return "plain"


def _url_meta(value: str) -> dict | None:
    """认证连接串的非秘密元数据（scheme/host/has_credentials）。

    host 用 ``urlsplit().hostname``（已剥 userinfo）；任何解析异常都回
    None——元数据是尽力而为的展示层，不是必须。
    """
    try:
        parts = urlsplit(value)
        if not parts.scheme:
            return None
        meta: dict = {"scheme": parts.scheme, "has_credentials": bool(parts.username)}
        host = parts.hostname
        if host:
            meta["host"] = host
        return meta
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 读取侧
# --------------------------------------------------------------------------- #


def _env_block(doc: dict) -> dict:
    env = doc.get(ENV_KEY)
    return env if isinstance(env, dict) else {}


def _versions_block(doc: dict) -> dict:
    v = doc.get(VERSIONS_KEY)
    return v if isinstance(v, dict) else {}


def _new_token() -> str:
    return secrets.token_hex(16)


def _sync_versions(env: dict, versions: dict) -> dict:
    """版本 token 与 env 键集合对齐：新键生成、消失键清除、未变键保留。

    保留未变键的 token 是关键——整文档 PUT /api/settings 没动 env 时，
    已打开的编辑器仍能用旧 token 完成 CAS（不因无关保存而全部失效）。
    """
    out = {}
    for key in env:
        prev = versions.get(key)
        prev = prev if isinstance(prev, dict) else {}
        token = prev.get("token")
        if not isinstance(token, str) or not token:
            token = _new_token()
        out[key] = {"token": token, "updated_at": prev.get("updated_at")}
    return out


def materialize_versions(doc: dict) -> dict | None:
    """为缺 token 的存量 env 键补版本元数据（惰性迁移，幂等）。

    返回补齐后的完整 doc；无需变更时返回 None（调用方免写）。
    """
    env = _env_block(doc)
    versions = _versions_block(doc)
    if not env and not versions:
        return None
    synced = _sync_versions(env, versions)
    if synced == versions:
        return None
    doc = dict(doc)
    if synced:
        doc[VERSIONS_KEY] = synced
    else:
        doc.pop(VERSIONS_KEY, None)
    return doc


def read_state() -> dict:
    """GET /api/settings/env 的数据源。

    - corrupt → ``{"status": "corrupt", "parseError": ...}``（entries 不回）；
    - 否则锁内惰性物化缺失 token（一次幂等迁移写，只增元数据不动值），
      返回每键视图：普通键带明文 value，敏感键只有元数据 + version。
    """
    doc = settings_store.load_doc()
    if doc.corrupt:
        return {"status": "corrupt", "parseError": doc.error, "entries": {}}

    def _mut(d: dict) -> dict | None:
        return materialize_versions(d)

    try:
        data = settings_store.update(_mut)
    except settings_store.SettingsCorruptError:
        # 竞态窗口内损坏：按损坏态呈现
        d2 = settings_store.load_doc()
        return {"status": "corrupt", "parseError": d2.error, "entries": {}}
    except settings_store.SettingsWriteError:
        # IO 故障：降级为只读视图（无 token 的键 version=null）
        log.warning("env_versions materialization failed; degraded read", exc_info=True)
        data = doc.data

    env = _env_block(data)
    versions = _versions_block(data)
    entries: dict[str, dict] = {}
    for key, value in env.items():
        token = (versions.get(key) or {}).get("token")
        if entry_is_sensitive(key, value):
            view: dict = {
                "sensitive": True,
                "set": True,
                "kind": _entry_kind(key, value),
                "version": token if isinstance(token, str) else None,
            }
            if view["kind"] == "url-with-auth" and isinstance(value, str):
                view["url"] = _url_meta(value)
            entries[key] = view
        else:
            entries[key] = {
                "sensitive": False,
                "kind": "plain",
                "value": value,
                "version": token if isinstance(token, str) else None,
            }
    return {"status": "ok", "entries": entries}


# --------------------------------------------------------------------------- #
# 写入侧（CAS）
# --------------------------------------------------------------------------- #


@dataclass
class EnvUpdate:
    key: str
    version: str | None
    value: Any = None
    remove: bool = False


def _parse_updates(payload: Any) -> list[EnvUpdate]:
    if not isinstance(payload, dict):
        raise EnvUpdateRejected("body must be a JSON object")
    updates = payload.get("updates")
    if not isinstance(updates, list) or not updates:
        raise EnvUpdateRejected("body.updates must be a non-empty list")
    seen: set[str] = set()
    out: list[EnvUpdate] = []
    for i, item in enumerate(updates):
        if not isinstance(item, dict):
            raise EnvUpdateRejected(f"updates[{i}] must be an object")
        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            raise EnvUpdateRejected(f"updates[{i}].key must be a non-empty string")
        key = key.strip()
        if "=" in key or "\x00" in key:
            raise EnvUpdateRejected(f"updates[{i}].key is not a valid env name: {key!r}")
        if key in seen:
            # 同键重复：无法定义合并语义，整个事务拒绝（400）
            raise EnvUpdateRejected(
                f"duplicate key in one request (all-or-nothing): {key}"
            )
        seen.add(key)
        version = item.get("version", None)
        if version is not None and not isinstance(version, str):
            raise EnvUpdateRejected(f"updates[{i}].version must be a string or null")
        remove = bool(item.get("remove", False))
        if remove:
            if "value" in item:
                raise EnvUpdateRejected(f"updates[{i}]: remove cannot carry a value")
            out.append(EnvUpdate(key=key, version=version, remove=True))
            continue
        if "value" not in item:
            raise EnvUpdateRejected(f"updates[{i}]: value (or remove:true) required")
        value = item["value"]
        if not isinstance(value, _ALLOWED_VALUE_TYPES) or isinstance(value, list):
            raise EnvUpdateRejected(
                f"updates[{i}].value must be a string/number/boolean "
                f"(got {type(value).__name__})"
            )
        out.append(EnvUpdate(key=key, version=version, value=value))
    return out


def apply_updates(payload: Any) -> dict:
    """全事务应用一批 env 更新。

    成功 → 返回新视图（含新 version token）；任一键 CAS 冲突 →
    :class:`EnvVersionConflict`（settings.json 未被触碰）。

    版本语义：
    - ``version=null``：创建语义——键必须当前不存在（含「键存在但尚无
      token」的存量键，冲突响应会带 current_version 供重试）；
    - ``version="<token>"``：键当前 token 必须精确匹配（token 每次写入
      重新生成，防 ABA）。
    """
    updates = _parse_updates(payload)

    def _mut(doc: dict) -> dict | None:
        env = dict(_env_block(doc))
        versions = _sync_versions(env, _versions_block(doc))
        conflicts: list[dict] = []
        staged: list[tuple[EnvUpdate, Any]] = []
        for u in updates:
            current = versions.get(u.key)
            current_token = (
                current.get("token") if isinstance(current, dict) else None
            )
            if u.version is None:
                if u.key in env:
                    conflicts.append(
                        {"key": u.key, "current_version": current_token}
                    )
                    continue
            else:
                if u.key not in env or current_token != u.version:
                    conflicts.append(
                        {"key": u.key, "current_version": current_token}
                    )
                    continue
            staged.append((u, current_token))
        if conflicts:
            raise EnvVersionConflict(conflicts)
        changed = False
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for u, _prev in staged:
            if u.remove:
                env.pop(u.key, None)
                versions.pop(u.key, None)
            else:
                env[u.key] = u.value
                versions[u.key] = {"token": _new_token(), "updated_at": now}
            changed = True
        if not changed:
            return None
        doc = dict(doc)
        if env:
            doc[ENV_KEY] = env
        else:
            doc.pop(ENV_KEY, None)
        if versions:
            doc[VERSIONS_KEY] = versions
        else:
            doc.pop(VERSIONS_KEY, None)
        return doc

    settings_store.update(_mut)  # corrupt → SettingsCorruptError（409 由 API 层转译）

    return read_state()


def after_full_document_write(doc: dict) -> None:
    """整文档 PUT /api/settings 落盘后的版本对齐钩子。

    env 键集合变化时同步增删 token；env 未变（掩码往返原样还原）时
    token 全部保留——已打开的 CAS 编辑器不因无关保存失效。
    """
    try:
        settings_store.update(materialize_versions)
    except (settings_store.SettingsCorruptError, settings_store.SettingsWriteError):
        log.warning("env_versions sync after full-document write failed", exc_info=True)
