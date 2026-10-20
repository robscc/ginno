"""会话映射表：钉钉 staffId/conversationId ↔ ginno session_id（设计 F2）。

F2 步骤 4：收到渠道消息后按 ``staffId``（P0 会话策略 ``per_user``）查映射表；
无映射或菜单 ``new_session=true`` 时创建新 ginno session 并 ``bind`` 写表。
映射 JSON 落盘于连接器数据目录（由调用方决定路径），结构：

.. code-block:: jsonc

    {"mappings": [{
      "staff_id": "01234", "conversation_id": "cid0...",
      "ginno_session": "a1b2c3...", "nick": "张三",
      "bound_at": "...", "last_active": "..."
    }]}

持久化策略：原子写（tmp + rename）防半截文件；内部 ``threading.Lock``
防并发 bind/unbind 竞态；文件损坏（非法 JSON / 非法结构）时备份后重建
空表——映射表只影响"接到哪个会话"，可安全重建，绝不让一条坏消息
炸掉渠道线程。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path


def _now() -> str:
    """本地时间 ISO 字符串（秒级），用于 bound_at / last_active。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SessionMapper:
    """钉钉身份 ↔ ginno session 的映射表（纯本地，JSON 持久化）。

    生命周期：适配器启动时 :meth:`load` 一次；此后每次写操作（bind/unbind）
    同步刷盘。键为 ``(staff_id, conversation_id)`` 组合——群聊与单聊即使同一
    staff 也各占一条映射（P0 ``per_user`` 下二者通常指向同一 session，由
    bind 端保证；本表只忠实记录）。
    """

    def __init__(self, store_path: Path) -> None:
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._mappings: list[dict] = []

    # ---- 持久化 ----------------------------------------------------------

    def load(self) -> None:
        """从 ``store_path`` 加载映射表；文件不存在或损坏时重建空表。

        损坏容错（F2 异常路径）：坏文件改名为 ``<name>.corrupt.bak`` 留档，
        内存置空、不抛异常——渠道启动不能被一张坏表卡死。
        """
        with self._lock:
            self._mappings = []
            if not self._path.exists():
                return
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                raw = data["mappings"]
                if not isinstance(raw, list):
                    raise ValueError(f"mappings is {type(raw).__name__}, not a list")
                self._mappings = [self._normalize(item) for item in raw]
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                self._backup_corrupt()
                self._mappings = []

    @staticmethod
    def _normalize(item: object) -> dict:
        """单条映射 → 规范字段 dict；字段缺失时补空串（不丢整条）。"""
        if not isinstance(item, dict):
            raise ValueError(f"mapping entry is {type(item).__name__}, not a dict")
        return {
            "staff_id": str(item.get("staff_id", "")),
            "conversation_id": str(item.get("conversation_id", "")),
            "ginno_session": str(item.get("ginno_session", "")),
            "nick": str(item.get("nick", "")),
            "bound_at": str(item.get("bound_at", "")),
            "last_active": str(item.get("last_active", "")),
        }

    def _backup_corrupt(self) -> None:
        """把损坏的 store 改名留档；改名失败（如权限）则忽略。"""
        try:
            self._path.rename(self._path.with_name(self._path.name + ".corrupt.bak"))
        except OSError:
            pass

    def _flush(self) -> None:
        """原子写盘：先写 ``.tmp`` 再 ``os.replace``（同目录 rename 原子）。"""
        payload = json.dumps({"mappings": self._mappings}, ensure_ascii=False, indent=2)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)

    # ---- 读写 API（F2 步骤 4）-------------------------------------------

    def get(self, *, staff_id: str, conversation_id: str) -> str | None:
        """查映射：命中返回 ginno session_id，未命中返回 None。"""
        with self._lock:
            for item in self._mappings:
                if item["staff_id"] == staff_id and item["conversation_id"] == conversation_id:
                    return item["ginno_session"]
            return None

    def bind(
        self,
        *,
        staff_id: str,
        conversation_id: str,
        ginno_session: str,
        nick: str = "",
    ) -> None:
        """upsert 一条映射并刷盘；重复 bind 更新 session/nick 与 last_active。"""
        with self._lock:
            now = _now()
            for item in self._mappings:
                if item["staff_id"] == staff_id and item["conversation_id"] == conversation_id:
                    item["ginno_session"] = ginno_session
                    item["nick"] = nick
                    item["last_active"] = now
                    break
            else:
                self._mappings.append(
                    {
                        "staff_id": staff_id,
                        "conversation_id": conversation_id,
                        "ginno_session": ginno_session,
                        "nick": nick,
                        "bound_at": now,
                        "last_active": now,
                    }
                )
            self._flush()

    def unbind(self, ginno_session: str) -> None:
        """删除所有指向 ``ginno_session`` 的映射（F2 异常：session 被删后重建）。

        按 ginno session 反查删除：同一 session 可能被多组身份映射引用。
        """
        with self._lock:
            self._mappings = [i for i in self._mappings if i["ginno_session"] != ginno_session]
            self._flush()

    def all(self) -> list[dict]:
        """全部映射的浅拷贝列表（用于状态展示/排障）。"""
        with self._lock:
            return [dict(item) for item in self._mappings]
