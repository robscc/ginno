"""Per-project artifact list, file-backed at artifacts_path(slug).

An artifact is anything produced or referenced worth surfacing in the
Artifacts panel: a file written, a doc, a workflow run, a link. The WS
layer auto-registers file/doc refs (from attach_ref) and the agent can
also register explicitly via the artifact_register tool.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .. import paths


def _read(slug: str) -> list[dict[str, Any]]:
    p = paths.artifacts_path(slug)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _write(slug: str, items: list[dict[str, Any]]) -> None:
    p = paths.artifacts_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def list_artifacts(slug: str, session_id: str | None = None) -> list[dict[str, Any]]:
    """List artifacts for a project, newest first.

    ``session_id=None`` returns everything (back-compat). Passing a session id
    scopes the Artifacts panel to that session's artifacts.
    """
    items = _read(slug)
    items.sort(key=lambda a: a.get("created", 0), reverse=True)
    if session_id is None:
        return items
    return [a for a in items if a.get("session_id") == session_id]


def add_artifact(
    slug: str, kind: str, name: str, ref: str = "", session_id: str | None = None
) -> dict[str, Any]:
    items = _read(slug)
    # de-dup by (kind, ref or name)
    key = (kind, ref or name)
    for it in items:
        if (it.get("kind"), it.get("ref") or it.get("name")) == key:
            return it
    item = {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "name": name,
        "ref": ref,
        "session_id": session_id,
        "created": time.time(),
    }
    items.append(item)
    _write(slug, items)
    return item


def delete_artifact(slug: str, artifact_id: str) -> bool:
    """Remove an artifact entry by exact id. Returns False if not found.

    Only the panel entry is deleted — any file on disk is left in place, so a
    mistaken delete is recoverable (re-attach / re-register re-surfaces it).
    """
    if not artifact_id:
        return False
    items = _read(slug)
    kept = [it for it in items if it.get("id") != artifact_id]
    if len(kept) == len(items):
        return False
    _write(slug, kept)
    return True


def get_artifact(slug: str, artifact_id: str) -> dict[str, Any] | None:
    if not artifact_id:
        return None
    for it in _read(slug):
        if it.get("id") == artifact_id:
            return it
    return None


def set_ref(slug: str, artifact_id: str, new_ref: str) -> dict[str, Any] | None:
    """Rewrite an artifact's ``ref`` in place (e.g. after its file moved).

    Must be an in-place edit rather than ``add_artifact`` under the new path:
    ``add_artifact`` de-dupes by ``(kind, ref or name)``, so re-adding with a
    changed ref would create a duplicate record instead of updating this one.
    """
    if not artifact_id:
        return None
    items = _read(slug)
    for it in items:
        if it.get("id") == artifact_id:
            it["ref"] = new_ref
            _write(slug, items)
            return it
    return None


# User-correctable fields only — id/created/session_id are system-managed,
# and anything outside this whitelist is silently ignored (foolproof PATCH).
_UPDATABLE = {"name", "kind", "ref", "schema"}


def update_artifact(
    slug: str, artifact_id: str, patch: dict[str, Any]
) -> dict[str, Any] | None:
    """Apply whitelisted edits to an artifact. Returns the updated item, or
    None if not found. Blank names are rejected (a nameless row is useless).
    ``schema`` holds a user-corrected summary that prompt injection prefers
    over the auto-computed one (see server._resolve_attached_files).
    """
    if not artifact_id:
        return None
    items = _read(slug)
    for it in items:
        if it.get("id") != artifact_id:
            continue
        name = patch.get("name")
        if name is not None and not str(name).strip():
            return None  # reject blank name; nothing else is applied
        for k, v in patch.items():
            if k in _UPDATABLE and v is not None:
                it[k] = str(v).strip() if k != "schema" else str(v)
        _write(slug, items)
        return it
    return None
