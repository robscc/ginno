"""TODO tools — let agents manage the global daily TODO list (right panel).

Availability per agent is gated by tools_allow patterns (see
registry.ensure_todo_tools): e.g. research gets only `todo_list`
(read-only), dev/writer get the full `todo_*` set.
"""

from __future__ import annotations

from .. import todos as todo_store

TODO_TOOL_NAMES = {"todo_list", "todo_create", "todo_update", "todo_done", "todo_delete"}
TODO_MUTATING = {"todo_create", "todo_update", "todo_done", "todo_delete"}


def _fmt(t: dict) -> str:
    mark = "x" if t.get("done") else " "
    pri = t.get("priority", "medium")
    cat = t.get("category") or ""
    due = t.get("due") or ""
    tail = " | ".join(x for x in (cat, due) if x)
    return f"[{t['id']}] [{mark}] ({pri}) {t['title']}" + (f"  | {tail}" if tail else "")


def todo_list() -> str:
    """List the current daily TODO items (with ids, for use in update/done)."""
    items = todo_store.list_todos()
    if not items:
        return "(empty)"
    return "\n".join(_fmt(t) for t in items)


def todo_create(title: str, priority: str = "medium", category: str = "", due: str = "") -> str:
    """Add a TODO item. priority in {high,medium,low}. Returns the new item incl. id."""
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    t = todo_store.create_todo({"title": title, "priority": priority, "category": category, "due": due})
    return "created " + _fmt(t)


def todo_update(todo_id: str, title: str = "", priority: str = "", category: str = "", due: str = "") -> str:
    """Edit fields of a TODO item. Empty string = leave unchanged."""
    patch = {k: v for k, v in {"title": title, "priority": priority, "category": category, "due": due}.items() if v}
    if priority and priority not in ("high", "medium", "low"):
        patch.pop("priority", None)
    t = todo_store.update_todo(todo_id, patch)
    return ("updated " + _fmt(t)) if t else f"not found: {todo_id}"


def todo_done(todo_id: str, done: bool = True) -> str:
    """Mark a TODO item done (default) or not done."""
    t = todo_store.update_todo(todo_id, {"done": bool(done)})
    return ("done " + _fmt(t)) if t else f"not found: {todo_id}"


def todo_delete(todo_id: str) -> str:
    """Delete a TODO item."""
    return "deleted" if todo_store.delete_todo(todo_id) else f"not found: {todo_id}"


# Wrap as LangChain tools (imported by graph.py)
from langchain_core.tools import tool as _tool  # noqa: E402

todo_list = _tool(todo_list)
todo_create = _tool(todo_create)
todo_update = _tool(todo_update)
todo_done = _tool(todo_done)
todo_delete = _tool(todo_delete)

ALL_TODO_TOOLS = [todo_list, todo_create, todo_update, todo_done, todo_delete]
