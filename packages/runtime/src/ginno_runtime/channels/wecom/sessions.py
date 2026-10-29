"""会话映射表：企业微信 userid/chatid ↔ ginno session_id。

结构与持久化策略同 channels/dingtalk/sessions.py（原子写 + 损坏备份重建），
仅字段名改为企微语境（user_id / chat_id）。键为组合：单聊 chat_id=user_id，
群聊/频道 chat_id=chatid，同一用户在不同群各占一条映射。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from ginno_runtime import jsonx as json


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SessionMapper:
    """企微身份 ↔ ginno session 的映射表（纯本地，JSON 持久化）。"""

    def __init__(self, store_path: Path) -> None:
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._mappings: list[dict] = []

    def load(self) -> None:
        """加载映射表；文件不存在或损坏（备份后）重建空表，不抛异常。"""
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
        if not isinstance(item, dict):
            raise ValueError(f"mapping entry is {type(item).__name__}, not a dict")
        return {
            "user_id": str(item.get("user_id", "")),
            "chat_id": str(item.get("chat_id", "")),
            "ginno_session": str(item.get("ginno_session", "")),
            "nick": str(item.get("nick", "")),
            "bound_at": str(item.get("bound_at", "")),
            "last_active": str(item.get("last_active", "")),
        }

    def _backup_corrupt(self) -> None:
        try:
            self._path.rename(self._path.with_name(self._path.name + ".corrupt.bak"))
        except OSError:
            pass

    def _flush(self) -> None:
        payload = json.dumps({"mappings": self._mappings}, ensure_ascii=False, indent=2)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, *, user_id: str, chat_id: str) -> str | None:
        with self._lock:
            for item in self._mappings:
                if item["user_id"] == user_id and item["chat_id"] == chat_id:
                    return item["ginno_session"]
            return None

    def bind(self, *, user_id: str, chat_id: str, ginno_session: str,
             nick: str = "") -> None:
        """upsert 一条映射并刷盘。"""
        with self._lock:
            now = _now()
            for item in self._mappings:
                if item["user_id"] == user_id and item["chat_id"] == chat_id:
                    item["ginno_session"] = ginno_session
                    item["nick"] = nick
                    item["last_active"] = now
                    break
            else:
                self._mappings.append({
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "ginno_session": ginno_session,
                    "nick": nick,
                    "bound_at": now,
                    "last_active": now,
                })
            self._flush()

    def unbind(self, ginno_session: str) -> None:
        with self._lock:
            self._mappings = [i for i in self._mappings
                              if i["ginno_session"] != ginno_session]
            self._flush()

    def chats_for(self, ginno_session: str) -> list[dict]:
        """反向查询：绑定到该 session 的全部映射条目（删除通知用）。"""
        with self._lock:
            return [dict(i) for i in self._mappings
                    if i["ginno_session"] == ginno_session]

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._mappings]
