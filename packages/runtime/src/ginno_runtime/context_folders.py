"""Context folder library — local directories mountable as session context.

Implements the folder-library half of docs/context-folders-design.md (M0,
decision C): a global registry ``~/.ginno/folders.json`` of user directories,
each with an access tier (``ro``/``rw``) and a rule-loading toggle. Sessions
reference entries by id (``context_folders`` + ``primary_folder`` in the
session meta); mounting is a per-session action, the library only remembers
the directories and their defaults.

Security stance (design §3.5, M0 decisions):
* ``access != config`` — a mounted folder grants file access only; nothing in
  it (settings, hooks, skills) is ever loaded. The sole exception is its
  AGENTS.md / GINNO.md rule text, and only when ``load_rules`` is on.
* The ``ro`` tier is a hard tool-layer constraint (write_file / edit_file
  refuse), independent of ``bypass_permissions``.
* Access OUTSIDE the mounted set keeps the pre-feature behaviour (M0 decision:
  status quo — no new containment).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from . import paths

# Recognized rule files, in precedence order (decision record #4: both are
# honored; AGENTS.md — the cross-product standard — wins when both exist).
RULE_FILES = ("AGENTS.md", "GINNO.md")

# Probe caps: never walk a huge tree synchronously on the API thread.
_PROBE_FILE_CAP = 2000
_PROBE_DIR_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv"}

# Per-folder rule-file injection budgets (design §4.2).
RULE_FILE_MAX_CHARS = 8000
RULES_TOTAL_MAX_CHARS = 24000

ACCESS_TIERS = ("ro", "rw")
DEFAULT_ACCESS = "rw"  # 2026-08-12 decision: mount is rw unless demoted


def folders_path() -> Path:
    return paths.home() / "folders.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def load_library() -> list[dict]:
    p = folders_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return []
    folders = data.get("folders")
    return folders if isinstance(folders, list) else []


def _save_library(folders: list[dict]) -> None:
    _atomic_write(
        folders_path(),
        json.dumps({"folders": folders}, indent=2, ensure_ascii=False),
    )


def norm_path(raw: str) -> str:
    """Expand + resolve a user-supplied path (symlink-safe comparison key)."""
    try:
        return str(Path(raw).expanduser().resolve())
    except OSError:
        return str(Path(raw).expanduser())


def get_folder(folder_id: str) -> dict | None:
    return next((f for f in load_library() if f.get("id") == folder_id), None)


def find_by_path(raw_path: str) -> dict | None:
    key = norm_path(raw_path)
    return next((f for f in load_library() if f.get("path") == key), None)


def add_folder(
    raw_path: str,
    name: str | None = None,
    access: str = DEFAULT_ACCESS,
    load_rules: bool = True,
) -> dict:
    """Register a directory. Idempotent on resolved path: re-adding an
    existing path returns the existing entry (updated fields win)."""
    real = norm_path(raw_path)
    folders = load_library()
    for f in folders:
        if f.get("path") == real:
            changed = False
            if name and name != f.get("name"):
                f["name"] = name
                changed = True
            if access in ACCESS_TIERS and access != f.get("access"):
                f["access"] = access
                changed = True
            if bool(load_rules) != bool(f.get("load_rules", True)):
                f["load_rules"] = bool(load_rules)
                changed = True
            if changed:
                _save_library(folders)
            return f
    entry = {
        "id": "f_" + secrets.token_hex(4),
        "path": real,
        "name": (name or Path(real).name or real),
        "access": access if access in ACCESS_TIERS else DEFAULT_ACCESS,
        "load_rules": bool(load_rules),
        "added": _now_iso(),
        "last_used": "",
    }
    folders.append(entry)
    _save_library(folders)
    return entry


def update_folder(folder_id: str, patch: dict) -> dict | None:
    folders = load_library()
    target = None
    for f in folders:
        if f.get("id") != folder_id:
            continue
        if "name" in patch and str(patch["name"] or "").strip():
            f["name"] = str(patch["name"]).strip()
        if "access" in patch and patch["access"] in ACCESS_TIERS:
            f["access"] = patch["access"]
        if "load_rules" in patch:
            f["load_rules"] = bool(patch["load_rules"])
        if "path" in patch and str(patch["path"] or "").strip():
            f["path"] = norm_path(str(patch["path"]))
        target = f
        break
    if target is None:
        return None
    _save_library(folders)
    return target


def remove_folder(folder_id: str) -> bool:
    folders = load_library()
    kept = [f for f in folders if f.get("id") != folder_id]
    if len(kept) == len(folders):
        return False
    _save_library(kept)
    return True


def touch_folder(folder_id: str) -> None:
    """Record usage time (best-effort; a failure only loses telemetry)."""
    try:
        folders = load_library()
        for f in folders:
            if f.get("id") == folder_id:
                f["last_used"] = _now_iso()
                _save_library(folders)
                return
    except Exception:
        pass


def probe(raw_path: str) -> dict:
    """Inspect a path for the add-folder UI (never raises)."""
    raw = (raw_path or "").strip()
    if not raw:
        return {"ok": False, "error": "路径为空"}
    real = norm_path(raw)
    p = Path(real)
    if not p.exists():
        return {"ok": False, "error": f"路径不存在：{real}", "path": real}
    if not p.is_dir():
        return {"ok": False, "error": f"不是目录：{real}", "path": real}
    file_count = 0
    truncated = False
    try:
        for root, dirs, files in os.walk(real):
            dirs[:] = [d for d in dirs if d not in _PROBE_DIR_SKIP]
            file_count += len(files)
            if file_count >= _PROBE_FILE_CAP:
                truncated = True
                break
    except OSError:
        pass
    rule_file = next((n for n in RULE_FILES if (p / n).is_file()), None)
    return {
        "ok": True,
        "path": real,
        "is_dir": True,
        "file_count": file_count,
        "file_count_truncated": truncated,
        "has_git": (p / ".git").exists(),
        "rule_file": rule_file,
        "already_registered": find_by_path(real) is not None,
    }


def rule_file_for(dir_path: str) -> Path | None:
    """The directory's rule file (AGENTS.md preferred), if present."""
    base = Path(dir_path)
    for n in RULE_FILES:
        cand = base / n
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def read_rule_text(dir_path: str) -> tuple[str, str] | None:
    """``(filename, truncated_text)`` for the directory's rule file, or None."""
    cand = rule_file_for(dir_path)
    if cand is None:
        return None
    try:
        text = cand.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > RULE_FILE_MAX_CHARS:
        text = text[:RULE_FILE_MAX_CHARS] + "\n…（规则文件过长，已截断）"
    return cand.name, text


def resolve_session_dirs(folder_ids: list[str], primary_id: str | None) -> tuple[list[dict], str | None]:
    """Resolve a session's mount ids into serializable context-dir dicts.

    Returns ``(dirs, primary_path)``. Unknown ids are kept with
    ``missing: True`` so the UI can surface them, but tool/injection consumers
    must skip missing entries. ``primary_path`` is None when there is no
    usable primary (unset or missing) — the session files dir stays the cwd.
    """
    lib = {f.get("id"): f for f in load_library()}
    dirs: list[dict] = []
    primary_path: str | None = None
    for fid in folder_ids or []:
        f = lib.get(fid)
        if f is None:
            dirs.append({"id": fid, "missing": True})
            continue
        real = f.get("path") or ""
        exists = Path(real).is_dir() if real else False
        d = {
            "id": fid,
            "path": real,
            "name": f.get("name") or Path(real).name,
            "access": f.get("access", DEFAULT_ACCESS),
            "load_rules": bool(f.get("load_rules", True)),
            "missing": not exists,
        }
        dirs.append(d)
        if fid == primary_id and exists:
            primary_path = real
    return dirs, primary_path
