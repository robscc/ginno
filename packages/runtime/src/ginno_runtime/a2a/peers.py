"""出站 peer 注册表（a2a-connector-design.md §5/§12.B）。

每个远端 A2A agent 一条 :class:`PeerRecord`（slug 形如 ``expense-bot``，
连接器 id 形如 ``a2a-expense-bot``）。存 ``a2a/peers.json``，原子写。

凭据纪律（SEC-01，照 api/config.py 惯例）：
- ``to_public()`` 输出掩码 token（``••••`` + 尾 4），明文绝不进 GET 响应；
- 掩码值回写视为「未修改」占位，忽略而非落盘——``••••`` 落盘后真实流量
  会带着它出网（2026-10-24 智谱 key 事故同型）。

删除守卫：peer 仍有未终态 task（contexts.json 影子状态）时拒绝删除，
raise :class:`PendingTaskError`（调用方转 409）。先取消/等终态，再删。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ginno_runtime import jsonx as json
from ginno_runtime import paths

# 与 providers.mask_secret / api/config.py SEC-01 同一惯例。自包含而非
# import providers：peer 存储不该拖起 provider 管理整条依赖链。
_MASK_PREFIX = "••••"

# A2A 终态（与 a2a/models.py TERMINAL_STATES 对齐：completed/failed/
# canceled/rejected）。自包含常量：不 import 并行开发中的协议模块，
# 存储层只依赖字符串状态值。input-required/auth-required 是非终态。
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})

_AUTH_SCHEMES = ("none", "bearer", "api_key", "oauth2")
_TRUST_LEVELS = ("readonly", "normal", "full")
_CONFIRM_MODES = ("every", "after_first", "always")


def _mask_token(value: str) -> str:
    """SEC-01 掩码：长 token 留尾 4，短 token 全掩，空保持空。"""
    if not value:
        return ""
    return _MASK_PREFIX + value[-4:] if len(value) > 4 else _MASK_PREFIX


def is_masked_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_MASK_PREFIX)


class PendingTaskError(Exception):
    """peer 仍有未终态 task，拒绝删除（调用方转 HTTP 409）。"""

    def __init__(self, slug: str, pending: list[dict]) -> None:
        self.slug = slug
        self.pending = pending  # [{key, task_id, state}, ...]
        super().__init__(
            f"peer {slug!r} has {len(pending)} non-terminal task(s); "
            "cancel or wait for them before removing")


@dataclass
class PeerRecord:
    """一个已接入的远端 agent。``id`` 由 slug 派生（``a2a-<slug>``），
    与设计 §5 的动态实例 id 一致。"""

    slug: str
    name: str = ""
    endpoint: str = ""
    token: str = ""
    peer_fingerprint: str = ""  # 地址 #fp 值（指纹钉死，§6.1）
    auth_scheme: str = "none"
    enabled: bool = True
    trust: str = "readonly"
    caps: list[str] = field(default_factory=lambda: ["L0"])
    task_timeout_s: int = 600
    max_concurrent: int = 2
    confirm_mode: str = "every"
    allow_local_files: bool = False
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"a2a-{self.slug}"
        if not self.name:
            self.name = self.slug

    def to_dict(self) -> dict:
        """落盘形态（明文 token——磁盘是凭据的唯一真源）。"""
        return {
            "slug": self.slug,
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "token": self.token,
            "peer_fingerprint": self.peer_fingerprint,
            "auth_scheme": self.auth_scheme,
            "enabled": self.enabled,
            "trust": self.trust,
            "caps": list(self.caps),
            "task_timeout_s": self.task_timeout_s,
            "max_concurrent": self.max_concurrent,
            "confirm_mode": self.confirm_mode,
            "allow_local_files": self.allow_local_files,
            "created_at": self.created_at,
        }

    def to_public(self) -> dict:
        """GET 响应形态：token 掩码（SEC-01），其余同 to_dict。"""
        out = self.to_dict()
        out["token"] = _mask_token(self.token)
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> PeerRecord:
        raw = raw if isinstance(raw, dict) else {}
        caps = raw.get("caps")
        return cls(
            slug=str(raw.get("slug") or ""),
            name=str(raw.get("name") or ""),
            endpoint=str(raw.get("endpoint") or ""),
            token=str(raw.get("token") or ""),
            peer_fingerprint=str(raw.get("peer_fingerprint") or ""),
            auth_scheme=str(raw.get("auth_scheme") or "none"),
            enabled=bool(raw.get("enabled", True)),
            trust=str(raw.get("trust") or "readonly"),
            caps=[str(c) for c in caps] if isinstance(caps, list) else ["L0"],
            task_timeout_s=int(raw.get("task_timeout_s") or 600),
            max_concurrent=int(raw.get("max_concurrent") or 2),
            confirm_mode=str(raw.get("confirm_mode") or "every"),
            allow_local_files=bool(raw.get("allow_local_files", False)),
            created_at=float(raw.get("created_at") or 0.0),
            id=str(raw.get("id") or ""),
        )


class PeerStore:
    """JSON 文件-backed peer 注册表。原子写（临时文件 + ``os.replace``），
    畸形 JSON 容错为空表。"""

    def __init__(self, path: Path | None = None, *,
                 contexts_path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.a2a_peers_path()
        self._contexts_path = contexts_path

    @property
    def path(self) -> Path:
        return self._path

    # ---- storage ----------------------------------------------------------

    def _load(self) -> dict[str, PeerRecord]:
        try:
            raw = json.loads(self._path.read_text() or "{}")
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, PeerRecord] = {}
        for value in raw.values():
            rec = PeerRecord.from_dict(value)
            if rec.slug:
                out[rec.slug] = rec
        return out

    def _save(self, records: dict[str, PeerRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({s: r.to_dict() for s, r in records.items()},
                             indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".peers-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- api ----------------------------------------------------------------

    def add(self, record: PeerRecord) -> PeerRecord:
        if not record.slug:
            raise ValueError("peer slug must not be empty")
        records = self._load()
        if record.slug in records:
            raise ValueError(f"peer {record.slug!r} already exists")
        records[record.slug] = record
        self._save(records)
        return record

    def find(self, slug: str) -> PeerRecord | None:
        return self._load().get(slug)

    def list_all(self) -> list[PeerRecord]:
        return sorted(self._load().values(), key=lambda r: r.slug)

    def update(self, slug: str, patch: dict) -> PeerRecord:
        """合并更新已知字段。掩码 token 视为「未修改」忽略（SEC-01）；
        未知键忽略（防拼写错误静默落盘）。slug 不存在 raise KeyError。"""
        records = self._load()
        rec = records.get(slug)
        if rec is None:
            raise KeyError(slug)
        data = rec.to_dict()
        for key, value in (patch or {}).items():
            if value is None or key not in data or key in ("slug", "id",
                                                           "created_at"):
                continue
            if key == "token":
                if is_masked_token(value):
                    continue  # 掩码占位 = 未修改,绝不落盘
                data[key] = str(value)
            elif key == "enabled" or key == "allow_local_files":
                data[key] = bool(value)
            elif key in ("task_timeout_s", "max_concurrent"):
                data[key] = int(value)
            elif key == "caps":
                data[key] = [str(c) for c in value] if isinstance(value, list) \
                    else data[key]
            elif key == "auth_scheme":
                data[key] = str(value) if value in _AUTH_SCHEMES else data[key]
            elif key == "trust":
                data[key] = str(value) if value in _TRUST_LEVELS else data[key]
            elif key == "confirm_mode":
                data[key] = str(value) if value in _CONFIRM_MODES else data[key]
            else:
                data[key] = str(value)
        updated = PeerRecord.from_dict(data)
        records[slug] = updated
        self._save(records)
        return updated

    def remove(self, slug: str) -> bool:
        """删除 peer。有未终态 task 时 raise :class:`PendingTaskError`。

        守卫读 contexts.json 的影子状态（零信任 §9.6：本地维护，不假设
        对端可查）。返回是否删除了记录。
        """
        records = self._load()
        if slug not in records:
            return False
        # 延迟 import:存储层之间保持单向依赖(peers → contexts),且避免
        # 包初始化顺序耦合。
        from .contexts import ContextMap

        cm = ContextMap(self._contexts_path
                        if self._contexts_path is not None
                        else paths.a2a_contexts_path())
        pending: list[dict] = []
        for rec in cm.list():
            if rec.peer_slug != slug:
                continue
            for task in rec.tasks:
                if task.state not in _TERMINAL_STATES:
                    pending.append({"key": rec.key, "task_id": task.task_id,
                                    "state": task.state})
        if pending:
            raise PendingTaskError(slug, pending)
        del records[slug]
        self._save(records)
        return True


__all__ = ["PeerRecord", "PeerStore", "PendingTaskError", "is_masked_token"]