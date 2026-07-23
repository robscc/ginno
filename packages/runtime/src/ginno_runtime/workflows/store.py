"""Workflow definitions (versioned DSL) + run instances, file-backed.

Layout (new, versioned)::

    ~/.ginno/workflows/<id>/meta.json          # {id,name,description,current,versions}
    ~/.ginno/workflows/<id>/versions/<n>.json  # full DSL snapshot (immutable)

Legacy single-file defs (``~/.ginno/workflows/<id>.json``) are migrated lazily on
first read (``get_def``/``list_defs``/``ensure_seeded``): the file is converted to
version 1 of the new layout and removed.

Every definition view carries a ``steps`` projection of the DSL nodes so the
existing ``workflow_*`` tools, right-panel tree and chat WorkflowBlock keep
working until the P2 graph executor supersedes them.

Runs stay global (``~/.ginno/workflow_runs/<run_id>.json``) and now pin the
``dsl_version`` they executed against, so old runs remain reproducible.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .. import paths
from . import dsl as wf_dsl


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


# ---- low-level io (atomic write lifted from checkpointer) ----
def _read_json(p: Path, default: Any) -> Any:
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text() or json.dumps(default))
    except json.JSONDecodeError:
        return default


def _write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


# ---- layout helpers ----
def _def_dir(wf_id: str) -> Path:
    return paths.workflows_dir() / wf_id


def _meta_path(wf_id: str) -> Path:
    return _def_dir(wf_id) / "meta.json"


def _versions_dir(wf_id: str) -> Path:
    return _def_dir(wf_id) / "versions"


def _version_path(wf_id: str, n: int) -> Path:
    return _versions_dir(wf_id) / f"{n}.json"


def _legacy_path(wf_id: str) -> Path:
    return paths.workflows_dir() / f"{wf_id}.json"


def _runs_dir() -> Path:
    return paths.home() / "workflow_runs"


def _run_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.json"


def _is_new_layout(wf_id: str) -> bool:
    return _meta_path(wf_id).exists()


def _is_legacy(wf_id: str) -> bool:
    return _legacy_path(wf_id).exists()


# ---- migration ----
def _migrate_legacy(wf_id: str) -> None:
    """Convert a legacy single-file def into version 1 of the new layout."""
    old = _read_json(_legacy_path(wf_id), None)
    if not isinstance(old, dict):
        return
    d = wf_dsl.legacy_steps_to_dsl(
        old.get("steps") or [],
        name=old.get("name", ""),
        description=old.get("description", ""),
    )
    d["name"] = old.get("name") or d.get("name") or "Untitled workflow"
    d["description"] = old.get("description", "")
    _write_json(_version_path(wf_id, 1), d)
    _write_json(
        _meta_path(wf_id),
        {
            "id": wf_id,
            "name": d["name"],
            "description": d["description"],
            "current": 1,
            "versions": [1],
        },
    )
    try:
        _legacy_path(wf_id).unlink()
    except OSError:
        pass


def _migrate_if_needed(wf_id: str) -> None:
    if _is_new_layout(wf_id):
        return
    if _is_legacy(wf_id):
        _migrate_legacy(wf_id)


# ---- dsl / version reads ----
def _read_version(wf_id: str, n: int) -> dict | None:
    v = _read_json(_version_path(wf_id, n), None)
    return v if isinstance(v, dict) else None


def _read_meta(wf_id: str) -> dict | None:
    m = _read_json(_meta_path(wf_id), None)
    return m if isinstance(m, dict) else None


def _current_dsl(wf_id: str) -> tuple[dict, int, dict] | None:
    meta = _read_meta(wf_id)
    if not meta:
        return None
    cur = meta.get("current", 1)
    d = _read_version(wf_id, cur)
    if d is None:
        return None
    return d, cur, meta


# ---- view assembly (always includes legacy `steps`) ----
def _build_view(wf_id: str, d: dict, current: int, meta: dict | None = None) -> dict:
    d = wf_dsl.normalize_dsl(d)
    # name/description are editable without bumping a DSL version, so the meta
    # file is authoritative for them; the immutable snapshot is the fallback.
    name = (meta or {}).get("name") or d.get("name") or "Untitled workflow"
    desc = (meta or {}).get("description", d.get("description", ""))
    return {
        "id": wf_id,
        "name": name,
        "description": desc,
        "current": current,
        "version": current,
        "dsl": d,
        "steps": wf_dsl.steps_from_dsl(d),
    }


# ---- definitions ----
def list_defs() -> list[dict[str, Any]]:
    paths.workflows_dir().mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    # legacy single files -> migrate
    for p in sorted(paths.workflows_dir().glob("*.json")):
        wf_id = p.stem
        _migrate_if_needed(wf_id)
    # new layout dirs
    for child in sorted(paths.workflows_dir().iterdir()):
        if not child.is_dir():
            continue
        wf_id = child.name
        triple = _current_dsl(wf_id)
        if not triple:
            continue
        d, cur, meta = triple
        out.append(_build_view(wf_id, d, cur, meta))
    return out


def get_def(wf_id: str) -> dict[str, Any] | None:
    _migrate_if_needed(wf_id)
    triple = _current_dsl(wf_id)
    if not triple:
        return None
    d, cur, meta = triple
    return _build_view(wf_id, d, cur, meta)


def create_def(data: dict[str, Any]) -> dict[str, Any]:
    wf_id = data.get("id") or _new_id()
    if _is_new_layout(wf_id) or _is_legacy(wf_id):
        raise ValueError(f"workflow {wf_id} already exists")
    raw = data.get("dsl")
    if isinstance(raw, dict):
        d = wf_dsl.normalize_dsl(raw)
        d["name"] = data.get("name") or d.get("name") or "Untitled workflow"
        d["description"] = data.get("description", "") if data.get("description") else d.get("description", "")
    else:
        d = wf_dsl.legacy_steps_to_dsl(
            data.get("steps") or [],
            name=data.get("name", ""),
            description=data.get("description", ""),
        )
    errs = wf_dsl.validate_dsl(d)
    if errs:
        raise ValueError("invalid DSL: " + "; ".join(errs))
    _write_json(_version_path(wf_id, 1), d)
    _write_json(
        _meta_path(wf_id),
        {
            "id": wf_id,
            "name": d["name"],
            "description": d["description"],
            "current": 1,
            "versions": [1],
        },
    )
    return get_def(wf_id)


def _write_version(wf_id: str, d: dict, commit: str = "") -> dict[str, Any] | None:
    """Append a new immutable version from a normalized DSL; advance current."""
    meta = _read_meta(wf_id)
    if not meta:
        return None
    d = wf_dsl.normalize_dsl(d)
    errs = wf_dsl.validate_dsl(d)
    if errs:
        raise ValueError("invalid DSL: " + "; ".join(errs))
    versions = sorted(meta.get("versions") or [])
    nxt = (versions[-1] + 1) if versions else 1
    _write_json(_version_path(wf_id, nxt), d)
    meta["versions"] = versions + [nxt]
    meta["current"] = nxt
    meta["name"] = d.get("name") or meta.get("name")
    meta["description"] = d.get("description", "")
    if commit:
        meta["last_commit"] = commit
    _write_json(_meta_path(wf_id), meta)
    return get_def(wf_id)


def update_def(wf_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Edit the current DSL. If `data` carries a `dsl`, it becomes a new version;
    else name/description-only edits mutate meta without a new DSL version."""
    _migrate_if_needed(wf_id)
    meta = _read_meta(wf_id)
    if not meta:
        return None
    raw = data.get("dsl")
    if isinstance(raw, dict):
        # carry over name/description if the payload didn't set them
        if "name" not in raw and data.get("name"):
            raw = {**raw, "name": data["name"]}
        if "description" not in raw and data.get("description") is not None:
            raw = {**raw, "description": data["description"]}
        return _write_version(wf_id, raw, commit=data.get("commit", ""))
    # meta-only edit
    if data.get("name"):
        meta["name"] = data["name"]
    if data.get("description") is not None:
        meta["description"] = data["description"]
    _write_json(_meta_path(wf_id), meta)
    return get_def(wf_id)


def delete_def(wf_id: str) -> bool:
    import shutil

    if _is_new_layout(wf_id):
        shutil.rmtree(_def_dir(wf_id), ignore_errors=True)
        return True
    if _is_legacy(wf_id):
        try:
            _legacy_path(wf_id).unlink()
            return True
        except OSError:
            return False
    return False


# ---- versions / diff / rollback ----
def list_versions(wf_id: str) -> list[dict[str, Any]]:
    _migrate_if_needed(wf_id)
    meta = _read_meta(wf_id)
    if not meta:
        return []
    cur = meta.get("current")
    return [{"version": n, "current": n == cur} for n in sorted(meta.get("versions") or [])]


def get_version(wf_id: str, n: int) -> dict[str, Any] | None:
    _migrate_if_needed(wf_id)
    return _read_version(wf_id, n)


def diff_versions(wf_id: str, a: int, b: int) -> str | None:
    va, vb = _read_version(wf_id, a), _read_version(wf_id, b)
    if va is None or vb is None:
        return None
    import difflib

    la = wf_dsl.canonical_dsl(va).splitlines(keepends=True)
    lb = wf_dsl.canonical_dsl(vb).splitlines(keepends=True)
    return "".join(difflib.unified_diff(la, lb, fromfile=f"v{a}", tofile=f"v{b}"))


def rollback(wf_id: str, to: int, commit: str = "") -> dict[str, Any] | None:
    """Create a new version whose DSL is a copy of version `to` (history kept)."""
    snap = _read_version(wf_id, to)
    if snap is None:
        return None
    return _write_version(wf_id, snap, commit=commit or f"rollback to v{to}")


# ---- runs (global; now version-pinned) ----
def list_runs() -> list[dict[str, Any]]:
    d = _runs_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        r = _read_json(p, None)
        if isinstance(r, dict):
            out.append(r)
    out.sort(key=lambda r: r.get("started", 0), reverse=True)
    return out


def get_run(run_id: str) -> dict[str, Any] | None:
    r = _read_json(_run_path(run_id), None)
    return r if isinstance(r, dict) else None


def _wf_dsl_and_version(wf: dict[str, Any]) -> tuple[dict, int]:
    """Accept a view (has dsl+version) or a raw/legacy dict (has steps)."""
    if isinstance(wf.get("dsl"), dict):
        return wf["dsl"], int(wf.get("version") or wf.get("current") or 1)
    return wf_dsl.legacy_steps_to_dsl(wf.get("steps") or []), 0


def create_run(wf: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
    now = time.time()
    d, ver = _wf_dsl_and_version(wf)
    steps = wf_dsl.steps_from_dsl(d)
    run = {
        "id": _new_id(),
        "workflow_id": wf.get("id", ""),
        "name": wf.get("name", ""),
        "session_id": session_id,
        "dsl_version": ver,
        "status": "running",
        "steps": [
            {"id": s["id"], "title": s.get("title", ""), "status": "pending", "output": ""}
            for s in steps
        ],
        "started": now,
        "updated": now,
    }
    _write_json(_run_path(run["id"]), run)
    return run


def update_step(run_id: str, step_id: str, status: str, output: str = "") -> dict[str, Any] | None:
    run = get_run(run_id)
    if not run:
        return None
    for s in run["steps"]:
        if s["id"] == step_id:
            s["status"] = status
            if output:
                s["output"] = output
    done = all(s["status"] in ("done", "failed") for s in run["steps"])
    run["status"] = "done" if done else "running"
    run["updated"] = time.time()
    _write_json(_run_path(run_id), run)
    return run


# ---- seed ----
_SEED = [
    {
        "id": "pr-triage",
        "name": "PR Triage",
        "description": "Triage open pull requests: list, review, summarise.",
        "steps": [
            {"id": "s1", "title": "List open PRs"},
            {"id": "s2", "title": "Review each PR"},
            {"id": "s3", "title": "Summarise findings"},
        ],
    }
]


def ensure_seeded() -> None:
    paths.workflows_dir().mkdir(parents=True, exist_ok=True)
    # migrate any legacy seed file first
    for wf in _SEED:
        _migrate_if_needed(wf["id"])
    for wf in _SEED:
        if not _is_new_layout(wf["id"]):
            create_def(wf)
