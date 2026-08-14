"""On-disk Space registry: ~/.ginno/browser/spaces.json + browser_state.json."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .. import paths
from .ownership import ALL_OWNERS, OWNER_AGENT


def browser_dir() -> Path:
    return paths.home() / "browser"


def profile_dir() -> Path:
    return browser_dir() / "profile"


def spaces_path() -> Path:
    return browser_dir() / "spaces.json"


def state_path() -> Path:
    return browser_dir() / "browser_state.json"


def ensure_browser_layout() -> None:
    browser_dir().mkdir(parents=True, exist_ok=True)
    profile_dir().mkdir(parents=True, exist_ok=True)
    (browser_dir() / "learnings").mkdir(parents=True, exist_ok=True)
    (browser_dir() / "downloads").mkdir(parents=True, exist_ok=True)
    if not spaces_path().exists():
        _atomic_write(spaces_path(), {"spaces": []})
    if not state_path().exists():
        _atomic_write(state_path(), {"active_space": None, "url": "", "focus": None})


def _atomic_write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _read_spaces() -> list[dict[str, Any]]:
    p = spaces_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return []
    spaces = raw.get("spaces") if isinstance(raw, dict) else None
    return list(spaces) if isinstance(spaces, list) else []


def _write_spaces(spaces: list[dict[str, Any]]) -> None:
    _atomic_write(spaces_path(), {"spaces": spaces})


def list_spaces() -> list[dict[str, Any]]:
    return [_normalize(s) for s in _read_spaces() if isinstance(s, dict)]


def get_space(name: str) -> dict[str, Any] | None:
    key = (name or "").strip()
    for s in _read_spaces():
        if isinstance(s, dict) and s.get("name") == key:
            return _normalize(s)
    return None


def upsert_space(record: dict[str, Any]) -> dict[str, Any]:
    name = (record.get("name") or "").strip()
    if not name:
        raise ValueError("space name required")
    spaces = _read_spaces()
    rec = _normalize({**record, "name": name, "updated_at": time.time()})
    for i, s in enumerate(spaces):
        if isinstance(s, dict) and s.get("name") == name:
            spaces[i] = rec
            _write_spaces(spaces)
            return rec
    rec.setdefault("created_at", rec["updated_at"])
    spaces.append(rec)
    _write_spaces(spaces)
    return rec


def delete_space(name: str) -> bool:
    key = (name or "").strip()
    spaces = _read_spaces()
    kept = [s for s in spaces if not (isinstance(s, dict) and s.get("name") == key)]
    if len(kept) == len(spaces):
        return False
    _write_spaces(kept)
    return True


def read_state() -> dict[str, Any]:
    p = state_path()
    if not p.exists():
        return {"active_space": None, "url": "", "focus": None}
    try:
        data = json.loads(p.read_text() or "{}")
        return data if isinstance(data, dict) else {"active_space": None, "url": "", "focus": None}
    except json.JSONDecodeError:
        return {"active_space": None, "url": "", "focus": None}


def write_state(state: dict[str, Any]) -> dict[str, Any]:
    out = {
        "active_space": state.get("active_space"),
        "url": state.get("url") or "",
        "focus": state.get("focus"),
    }
    _atomic_write(state_path(), out)
    return out


def _normalize(s: dict[str, Any]) -> dict[str, Any]:
    owner = s.get("owner") if s.get("owner") in ALL_OWNERS else OWNER_AGENT
    return {
        "name": s.get("name") or "",
        "owner": owner,
        "bound_run_id": s.get("bound_run_id"),
        "bound_session_id": s.get("bound_session_id"),
        "created_at": s.get("created_at") or time.time(),
        "updated_at": s.get("updated_at") or s.get("created_at") or time.time(),
        "url": s.get("url") or "",
        "title": s.get("title") or "",
        "tabs": list(s.get("tabs") or []),
        "keep": bool(s.get("keep", True)),
        "reason": s.get("reason") or "",
        "headed": bool(s.get("headed", False)),
        "pending_risky_url": s.get("pending_risky_url") or "",
    }
