"""Global daily TODO store — ~/.ginno/todos.json.

Schema per item:
    { id, title, priority: high|medium|low, category, due, done,
      links: {session_id?, workflow_id?}, ext: [{provider, id, url?, …}],
      created, completed_at? }

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


def _read() -> list[dict[str, Any]]:
    p = paths.todos_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


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


def list_todos() -> list[dict[str, Any]]:
    return _read()


def create_todo(data: dict[str, Any]) -> dict[str, Any]:
    items = _read()
    item = {
        "id": _new_id(),
        "title": (data.get("title") or "").strip(),
        "priority": data.get("priority") or "medium",
        "category": data.get("category") or "",
        "due": data.get("due") or "",
        "done": bool(data.get("done", False)),
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
            allowed = {"title", "priority", "category", "due", "done", "links", "ext"}
            for k, v in patch.items():
                if k in allowed and v is not None:
                    if k == "links" and isinstance(v, dict):
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


_SEED: list[dict[str, Any]] = [
    {"title": "Review PR #234 - Fix auth bug", "priority": "high", "category": "Dev", "due": "14:00", "done": False},
    {"title": "Review PR #231 - API refactor", "priority": "high", "category": "Dev", "due": "16:00", "done": False},
    {"title": "Draft Q2 roadmap brief", "priority": "medium", "category": "PM", "due": "EOD", "done": False},
    {"title": "Update API documentation", "priority": "medium", "category": "Dev", "due": "Tomorrow", "done": False},
    {"title": "Prepare sprint review PPT", "priority": "low", "category": "Design", "due": "", "done": False},
    {"title": "Set up staging environment", "priority": "low", "category": "Dev", "due": "", "done": True},
    {"title": "Audit CI/CD pipeline", "priority": "low", "category": "Dev", "due": "", "done": True},
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
            "links": {},
            "created": now,
            "completed_at": (now - 3600) if s["done"] else None,
            "title": s["title"],
        }
        for s in _SEED
    ]
    _write(items)
