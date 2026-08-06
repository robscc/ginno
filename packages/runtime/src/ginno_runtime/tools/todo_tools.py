"""TODO tools — let agents manage the global daily TODO list (right panel).

Availability per agent is gated by tools_allow patterns (see
registry.ensure_todo_tools): e.g. research gets only `todo_list`
(read-only), dev/writer get the full `todo_*` set.
"""

from __future__ import annotations

from typing import Any

from .. import todos as todo_store

TODO_TOOL_NAMES = {
    "todo_list",
    "todo_create",
    "todo_update",
    "todo_done",
    "todo_delete",
    "todo_link",
}
TODO_MUTATING = {
    "todo_create",
    "todo_update",
    "todo_done",
    "todo_delete",
    "todo_link",
}


def _fmt(t: dict) -> str:
    mark = "x" if t.get("done") else " "
    pri = t.get("priority", "medium")
    emoji = (t.get("emoji") or "").strip()
    cat = t.get("category") or ""
    due = t.get("due") or ""
    tags = t.get("tags") or []
    n_sessions = len(t.get("session_ids") or [])
    n_artifacts = len(t.get("artifact_ids") or [])
    tail = " | ".join(x for x in (cat, due) if x)
    for x in t.get("ext") or []:
        if x.get("provider"):
            tail = " | ".join(x for x in (tail, f"ext:{x.get('provider')}:{x.get('id')}") if x)
    if tags:
        tail += (" | " if tail else "") + " ".join(f"#{x}" for x in tags)
    if n_sessions or n_artifacts:
        links = []
        if n_sessions:
            links.append(f"{n_sessions} session(s)")
        if n_artifacts:
            links.append(f"{n_artifacts} artifact(s)")
        tail += (" | " if tail else "") + "linked: " + ", ".join(links)
    title = f"{emoji} {t['title']}" if emoji else t["title"]
    return f"[{t['id']}] [{mark}] ({pri}) {title}" + (f"  | {tail}" if tail else "")


def todo_list() -> str:
    """List the current daily TODO items (with ids, for use in update/done/link)."""
    items = todo_store.list_todos()
    if not items:
        return "(empty)"
    return "\n".join(_fmt(t) for t in items)


def todo_create(
    title: str,
    priority: str = "medium",
    category: str = "",
    due: str = "",
    emoji: str = "",
    tags: str = "",
    ext: Any = None,
) -> str:
    """Add a TODO item. priority in {high,medium,low}; emoji = optional icon
    shown before the title (single emoji); tags = space/comma-separated labels
    (e.g. "review urgent"). `ext` links the item to external TODO-platform
    twins for bidirectional sync, e.g.
    [{"provider": "dingtalk", "id": "<platform todo id>"}]. Returns the new
    item incl. id."""
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    t = todo_store.create_todo(
        {"title": title, "priority": priority, "category": category, "due": due, "emoji": emoji, "tags": tags, "ext": ext or []}
    )
    return "created " + _fmt(t)


def todo_update(
    todo_id: str,
    title: str = "",
    priority: str = "",
    category: str = "",
    due: str = "",
    emoji: str = "",
    tags: str = "",
    ext: Any = None,
) -> str:
    """Edit fields of a TODO item. Empty string = leave unchanged. tags
    replaces the whole tag list (space/comma-separated). `ext` replaces the
    external ref list (e.g. attach [{"provider": "dingtalk", "id": ...}])."""
    patch = {k: v for k, v in {"title": title, "priority": priority, "category": category, "due": due}.items() if v}
    if priority and priority not in ("high", "medium", "low"):
        patch.pop("priority", None)
    if emoji:
        patch["emoji"] = emoji
    if tags:
        patch["tags"] = tags
    if ext is not None:
        patch["ext"] = ext
    t = todo_store.update_todo(todo_id, patch)
    return ("updated " + _fmt(t)) if t else f"not found: {todo_id}"


def todo_done(todo_id: str, done: bool = True) -> str:
    """Mark a TODO item done (default) or not done."""
    t = todo_store.update_todo(todo_id, {"done": bool(done)})
    return ("done " + _fmt(t)) if t else f"not found: {todo_id}"


def todo_delete(todo_id: str) -> str:
    """Delete a TODO item."""
    return "deleted" if todo_store.delete_todo(todo_id) else f"not found: {todo_id}"


def todo_link(todo_id: str, artifact_id: str = "", session_id: str = "", unlink: bool = False) -> str:
    """Associate a TODO with a deliverable (artifact_id) and/or a session
    (session_id). Pass unlink=true to remove the association instead. The
    sessions where an item is mentioned/completed are also linked
    automatically — use this mainly for artifacts you produced for the item."""
    if not artifact_id and not session_id:
        return "nothing to link: pass artifact_id and/or session_id"
    if todo_store.get_todo(todo_id) is None:
        return f"not found: {todo_id}"
    if artifact_id:
        fn = todo_store.unlink_artifact if unlink else todo_store.link_artifact
        fn(todo_id, artifact_id)
    if session_id:
        fn = todo_store.unlink_session if unlink else todo_store.link_session
        fn(todo_id, session_id)
    t = todo_store.get_todo(todo_id)
    return ("unlinked " if unlink else "linked ") + _fmt(t)


# Wrap as LangChain tools (imported by graph.py)
from langchain_core.tools import tool as _tool  # noqa: E402

todo_list = _tool(todo_list)
todo_create = _tool(todo_create)
todo_update = _tool(todo_update)
todo_done = _tool(todo_done)
todo_delete = _tool(todo_delete)
todo_link = _tool(todo_link)

ALL_TODO_TOOLS = [todo_list, todo_create, todo_update, todo_done, todo_delete, todo_link]
