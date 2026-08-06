"""Global daily TODO store — ~/.ginno/todos.json.

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
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .. import paths

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Fields a caller may set on create/update (beyond id/created/completed_at).
_WRITABLE = {"title", "priority", "category", "due", "done", "links", "emoji", "tags", "session_ids", "artifact_ids", "ext"}


def _read() -> list[dict[str, Any]]:
    p = paths.todos_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
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
    paths.todos_path().write_text(json.dumps(items, indent=2, ensure_ascii=False))


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
    return [_normalize(it) for it in _read()]


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
    items.append(item)
    _write(items)
    return item


def update_todo(todo_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
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
    """Seed the mock daily list when the store is empty (first run only)."""
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
