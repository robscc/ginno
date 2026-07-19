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


def list_artifacts(slug: str) -> list[dict[str, Any]]:
    items = _read(slug)
    items.sort(key=lambda a: a.get("created", 0), reverse=True)
    return items


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
