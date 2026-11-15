"""出站 context 映射表（a2a-connector-design.md §8.2）。

Ginno session × peer → 远端 ``contextId`` 的 1:1 映射，外加该 context 下
task 的轻量状态影子（``TaskBrief``）。存独立 ``a2a/contexts.json``——
checkpoint 是会话内容，映射是连接器状态，职责分离（§8.2）。

key 形如 ``f"{ginno_sid}:{peer_slug}"``：同一 Ginno session 委托多个 peer
时各持一条记录（§8.2 的 (S, expense-bot)→C1 / (S, legal-review)→C2）。

本模块只做存储与映射，**不发网络请求**：会话删除时的连带 CancelTask 由
工具层负责（``remove_session`` 返回被删 key 供其遍历）。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ginno_runtime import jsonx as json
from ginno_runtime import paths


def _key(ginno_sid: str, peer_slug: str) -> str:
    return f"{ginno_sid}:{peer_slug}"


@dataclass
class TaskBrief:
    """task 的本地影子状态（零信任防御契约 §9.6：不假设对端遵守 TTL）。"""

    task_id: str
    state: str = "submitted"
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "state": self.state,
                "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, raw: dict) -> TaskBrief:
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            task_id=str(raw.get("task_id") or ""),
            state=str(raw.get("state") or "submitted"),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


@dataclass
class ContextRecord:
    """一条 (session, peer) → contextId 映射。

    ``context_id`` 为空 = 壳记录（远端 context 已重置，等待下次委托开新
    context，见 :meth:`ContextMap.record_peer_reset`）。
    """

    ginno_sid: str
    peer_slug: str
    context_id: str = ""
    tasks: list[TaskBrief] = field(default_factory=list)
    last_used: float = field(default_factory=time.time)
    created: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return _key(self.ginno_sid, self.peer_slug)

    def to_dict(self) -> dict:
        return {
            "ginno_sid": self.ginno_sid,
            "peer_slug": self.peer_slug,
            "context_id": self.context_id,
            "tasks": [t.to_dict() for t in self.tasks],
            "last_used": self.last_used,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> ContextRecord:
        raw = raw if isinstance(raw, dict) else {}
        tasks = [TaskBrief.from_dict(t) for t in (raw.get("tasks") or [])
                 if isinstance(t, dict)]
        return cls(
            ginno_sid=str(raw.get("ginno_sid") or ""),
            peer_slug=str(raw.get("peer_slug") or ""),
            context_id=str(raw.get("context_id") or ""),
            tasks=tasks,
            last_used=float(raw.get("last_used") or 0.0),
            created=float(raw.get("created") or 0.0),
        )


class ContextMap:
    """JSON 文件-backed 映射表。原子写（临时文件 + ``os.replace``），
    畸形 JSON 容错为空表（连接器状态损坏不该拖垮会话）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.a2a_contexts_path()

    @property
    def path(self) -> Path:
        return self._path

    # ---- storage ----------------------------------------------------------

    def _load(self) -> dict[str, ContextRecord]:
        try:
            raw = json.loads(self._path.read_text() or "{}")
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, ContextRecord] = {}
        for key, value in raw.items():
            rec = ContextRecord.from_dict(value)
            # 以记录自身字段重建 key,容忍文件里 key 与内容不一致的脏数据。
            if rec.ginno_sid and rec.peer_slug:
                out[rec.key] = rec
        return out

    def _save(self, records: dict[str, ContextRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({k: r.to_dict() for k, r in records.items()},
                             indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".contexts-", suffix=".tmp")
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

    def get_or_none(self, key: str) -> ContextRecord | None:
        return self._load().get(key)

    def set(self, record: ContextRecord) -> ContextRecord:
        """写入/覆盖一条映射（委托成功拿到 contextId 后调用）。"""
        records = self._load()
        records[record.key] = record
        self._save(records)
        return record

    def update_task_state(self, key: str, task_id: str, state: str) -> bool:
        """更新 task 影子状态；task 不存在则追加。key 不存在时 no-op。

        状态迁移算「活动」（§9.2 滑动续期触发点），顺带刷新 last_used。
        返回是否命中记录。
        """
        records = self._load()
        rec = records.get(key)
        if rec is None:
            return False
        now = time.time()
        for task in rec.tasks:
            if task.task_id == task_id:
                task.state = state
                task.updated_at = now
                break
        else:
            rec.tasks.append(TaskBrief(task_id=task_id, state=state,
                                       updated_at=now))
        rec.last_used = now
        self._save(records)
        return True

    def remove_session(self, ginno_sid: str) -> list[str]:
        """删除该 session 的全部映射（多 peer 连带），返回被删 key 列表。

        只删本地记录；远端 CancelTask 由工具层拿返回值去发（§8.5）。
        """
        records = self._load()
        removed = [k for k, r in records.items() if r.ginno_sid == ginno_sid]
        if removed:
            for k in removed:
                del records[k]
            self._save(records)
        return removed

    def record_peer_reset(self, key: str) -> bool:
        """远端 context 失效自愈（§9.6）：清 context_id 与旧 tasks，留壳。

        壳保留 (session, peer) 关系；下次委托**省略 contextId 重发**让服务端
        生成新的，成功后 :meth:`set` 回填。旧 tasks 属于已失效 context，
        一并清空——否则非终态幽灵 task 会永远挡住 peer 删除守卫。
        """
        records = self._load()
        rec = records.get(key)
        if rec is None:
            return False
        rec.context_id = ""
        rec.tasks = []
        rec.last_used = time.time()
        self._save(records)
        return True

    def list(self) -> list[ContextRecord]:
        return sorted(self._load().values(), key=lambda r: r.key)


__all__ = ["ContextMap", "ContextRecord", "TaskBrief"]