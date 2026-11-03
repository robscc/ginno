"""Global TODO store — ~/.ginno/todos.json.

Schema per item:
    { id, title, priority: high|medium|low, category, due, done,
      emoji, tags: [str], session_ids: [str], artifact_ids: [str],
      links: {session_id?, workflow_id?}, ext: [{provider, id, url?, …}],
      created, completed_at? }

``emoji`` is an optional icon rendered before the title; ``tags`` are
free-form labels. ``session_ids`` / ``artifact_ids`` associate the item
with the sessions where it was mentioned/worked on and with concrete
deliverables (artifacts belong to sessions via their own ``session_id``).
Session linkage is added automatically by the WS layer whenever a turn
touches the item; artifact linkage is explicit (todo_link tool / UI).

`ext` is the (loose) external-ref list: one entry per attached TODO platform
(DingTalk, TODO-list, …); unknown keys are preserved for forward compat.
`progress` is derived by the client (done / visible total).

数据安全（2026-11-03 事故修复：「TODO 清单被清理没了」）：写走 tmp+rename
原子替换；读遇损坏文件先挪去 ``todos.json.corrupt-<ts>`` 保留现场再从空表
继续——旧版非原子写 + 解析失败静默返回 []，一次半截 JSON 就让后续 create
把只剩新条目的列表整表写回，旧数据无痕蒸发。读写改写全部过进程内锁
（多个 turn 并发操作 todo 是常态——定时任务与用户会话并存）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from ginno_runtime import jsonx as json

from .. import paths

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Fields a caller may set on create/update (beyond id/created/completed_at).
_WRITABLE = {"title", "priority", "category", "due", "done", "links", "emoji", "tags", "session_ids", "artifact_ids", "ext"}

# 串行化所有 read-modify-write。两层：进程内 threading 锁保同进程多任务；
# flock 旁锁文件保跨进程（桌面 sidecar 与 dev sidecar 双进程并存是真实
# 场景——standby 机制本身就是为它设计的）。fcntl 不可用时退化为进程内。
class _TodoLock:
    """flock(todos.json.lock) + threading 锁的组合，用法 ``with _LOCK:``。"""

    def __init__(self) -> None:
        self._tlock = threading.Lock()
        self._fh: Any = None

    def __enter__(self) -> "_TodoLock":
        self._tlock.acquire()
        try:
            import fcntl

            self._fh = open(str(paths.todos_path()) + ".lock", "a")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            self._fh = None  # 非 POSIX / 锁文件打不开：退化为进程内互斥
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._fh is not None:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None
        finally:
            self._tlock.release()


_LOCK = _TodoLock()

# 已完成条目的保留期（用户 2026-11-03 定：完成超过 1 周自动清理）。
_DONE_TTL_S = 7 * 86400


def _read() -> list[dict[str, Any]]:
    p = paths.todos_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        # 损坏文件（半截 JSON 等）先保留现场再从空表继续——直接返回 [] 会让
        # 下一次 create 把旧数据整表覆盖掉（2026-11-03 清单蒸发事故路径）。
        try:
            p.rename(p.with_name(p.name + f".corrupt-{int(time.time())}"))
        except OSError:
            pass
        return []


def _normalize(it: dict[str, Any]) -> dict[str, Any]:
    """Backfill defaults for fields added after the item was written."""
    it.setdefault("emoji", "")
    it.setdefault("tags", [])
    it.setdefault("session_ids", [])
    it.setdefault("artifact_ids", [])
    it.setdefault("links", {})
    it.setdefault("ext", [])
    return it


def _write(items: list[dict[str, Any]]) -> None:
    # 原子写：tmp + os.replace，杜绝并发/中断留下半截 JSON。
    p = paths.todos_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def norm_ext(v: Any) -> list[dict[str, Any]]:
    """Normalize ext to a list of ref dicts (dict / list / JSON-string accepted;
    unknown keys preserved). Models often stringify the list, so parse it.
    Dedup key for callers is (provider, id)."""
    if not v:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v.strip())
        except json.JSONDecodeError:
            return []
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict) and (x.get("provider") or x.get("id"))]


def _as_tag_list(v: Any) -> list[str]:
    """Accept list/tuple or a comma/space-separated string; trim + dedupe."""
    if v is None:
        return []
    if isinstance(v, str):
        parts = v.replace(",", " ").replace("，", " ").split()
    elif isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        return []
    out: list[str] = []
    for p in parts:
        p = p.strip().strip("#")
        if p and p not in out:
            out.append(p)
    return out[:8]


def list_todos() -> list[dict[str, Any]]:
    # 惰性清理：完成超 TTL 的条目在读时顺带清掉（server 启动时也会跑一次）。
    purge_stale_done()
    with _LOCK:
        return [_normalize(it) for it in _read()]


def purge_stale_done(max_age_s: float = _DONE_TTL_S) -> int:
    """已完成超过 ``max_age_s`` 的条目自动清理，返回清理条数。

    用户 2026-11-03 定：完成超过 1 周即清。未完成条目永不过期；缺
    completed_at 的完成条目按 created 折算（老数据兜底）。
    """
    with _LOCK:
        items = _read()
        now = time.time()
        kept = []
        removed = 0
        for it in items:
            if it.get("done"):
                done_at = it.get("completed_at") or it.get("created") or 0
                try:
                    age = now - float(done_at)
                except (TypeError, ValueError):
                    age = 0.0
                if age > max_age_s:
                    removed += 1
                    continue
            kept.append(it)
        if removed:
            _write(kept)
        return removed


def get_todo(todo_id: str) -> dict[str, Any] | None:
    for it in list_todos():
        if it.get("id") == todo_id:
            return it
    return None


def create_todo(data: dict[str, Any]) -> dict[str, Any]:
    items = _read()
    item = {
        "id": _new_id(),
        "title": (data.get("title") or "").strip(),
        "priority": data.get("priority") or "medium",
        "category": data.get("category") or "",
        "due": data.get("due") or "",
        "done": bool(data.get("done", False)),
        "emoji": (data.get("emoji") or "").strip(),
        "tags": _as_tag_list(data.get("tags")),
        "session_ids": _as_tag_list(data.get("session_ids")),
        "artifact_ids": _as_tag_list(data.get("artifact_ids")),
        "links": data.get("links") or {},
        "ext": norm_ext(data.get("ext")),
        "created": time.time(),
        "completed_at": time.time() if data.get("done") else None,
    }
    if not item["title"]:
        raise ValueError("title required")
    with _LOCK:
        items = _read()
        items.append(item)
        _write(items)
    return item


def update_todo(todo_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        items = _read()
        target = None
        for it in items:
            if it.get("id") == todo_id:
                _normalize(it)
                for k, v in patch.items():
                    if k not in _WRITABLE or v is None:
                        continue
                    if k in ("tags", "session_ids", "artifact_ids"):
                        it[k] = _as_tag_list(v)
                    elif k == "emoji":
                        it[k] = str(v).strip()
                    elif k == "links" and isinstance(v, dict):
                        # merge so partial stamps never wipe sibling keys
                        it.setdefault(k, {}).update(v)
                    elif k == "ext":
                        it[k] = norm_ext(v)
                    else:
                        it[k] = v
                # maintain completed_at
                if "done" in patch:
                    it["completed_at"] = time.time() if patch["done"] else None
                target = it
        if target is None:
            return None
        _write(items)
        return target


def delete_todo(todo_id: str) -> bool:
    with _LOCK:
        items = _read()
        kept = [it for it in items if it.get("id") != todo_id]
        if len(kept) == len(items):
            return False
        _write(kept)
        return True


# ---- association helpers -------------------------------------------------

def _link(todo_id: str, field: str, value: str, remove: bool = False) -> dict[str, Any] | None:
    """Add/remove ``value`` in ``todo[field]`` (dedup'd list)."""
    value = (value or "").strip()
    if not todo_id or not value:
        return get_todo(todo_id) if todo_id else None
    with _LOCK:
        items = _read()
        target = None
        for it in items:
            if it.get("id") == todo_id:
                _normalize(it)
                ids: list[str] = list(it.get(field) or [])
                if remove:
                    ids = [x for x in ids if x != value]
                elif value not in ids:
                    ids.append(value)
                it[field] = ids
                target = it
        if target is None:
            return None
        _write(items)
        return target


def link_session(todo_id: str, session_id: str) -> dict[str, Any] | None:
    return _link(todo_id, "session_ids", session_id)


def unlink_session(todo_id: str, session_id: str) -> dict[str, Any] | None:
    return _link(todo_id, "session_ids", session_id, remove=True)


def link_artifact(todo_id: str, artifact_id: str) -> dict[str, Any] | None:
    return _link(todo_id, "artifact_ids", artifact_id)


def unlink_artifact(todo_id: str, artifact_id: str) -> dict[str, Any] | None:
    return _link(todo_id, "artifact_ids", artifact_id, remove=True)


# Items whose observable state differs from ``prev`` (a prior
# ``list_todos()`` snapshot): newly created ids plus any field change.
# The WS layer uses this after each turn to auto-associate the session
# the agent was working in with every TODO it created/touched. The
# *_ids lists count too: linking an artifact/session from a turn means
# the item was "mentioned" in that turn's session.
_COMPARE_FIELDS = (
    "title", "priority", "category", "due", "done", "emoji", "tags",
    "links", "artifact_ids", "session_ids",
)


def touched_since(prev: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prev_by_id = {p.get("id"): p for p in prev}
    touched = []
    for it in list_todos():
        old = prev_by_id.get(it.get("id"))
        if old is None or any(it.get(f) != old.get(f) for f in _COMPARE_FIELDS):
            touched.append(it)
    return touched


_SEED: list[dict[str, Any]] = [
    {"title": "Review PR #234 - Fix auth bug", "priority": "high", "category": "Dev", "due": "14:00", "done": False, "emoji": "🐛", "tags": ["review"]},
    {"title": "Review PR #231 - API refactor", "priority": "high", "category": "Dev", "due": "16:00", "done": False, "emoji": "🔍", "tags": ["review"]},
    {"title": "Draft Q2 roadmap brief", "priority": "medium", "category": "PM", "due": "EOD", "done": False, "emoji": "🗺️", "tags": ["roadmap"]},
    {"title": "Update API documentation", "priority": "medium", "category": "Dev", "due": "Tomorrow", "done": False, "emoji": "📝", "tags": ["docs"]},
    {"title": "Prepare sprint review PPT", "priority": "low", "category": "Design", "due": "", "done": False, "emoji": "📊", "tags": ["meeting"]},
    {"title": "Set up staging environment", "priority": "low", "category": "Dev", "due": "", "done": True, "emoji": "🚀", "tags": ["infra"]},
    {"title": "Audit CI/CD pipeline", "priority": "low", "category": "Dev", "due": "", "done": True, "emoji": "⚙️", "tags": ["infra", "ci"]},
]


def ensure_seeded() -> None:
    """Seed the mock list when the store is empty (first run only); also runs
    the stale-done purge (server startup touchpoint)."""
    purge_stale_done()
    if _read():
        return
    now = time.time()
    items = [
        {
            "id": _new_id(),
            "priority": s["priority"],
            "category": s["category"],
            "due": s["due"],
            "done": s["done"],
            "emoji": s.get("emoji", ""),
            "tags": s.get("tags", []),
            "session_ids": [],
            "artifact_ids": [],
            "links": {},
            "created": now,
            "completed_at": (now - 3600) if s["done"] else None,
            "title": s["title"],
        }
        for s in _SEED
    ]
    _write(items)
