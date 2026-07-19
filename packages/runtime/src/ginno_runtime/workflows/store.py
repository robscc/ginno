"""Workflow definitions + run instances, file-backed.

Defs:   ~/.ginno/workflows/<id>.json
Runs:   ~/.ginno/projects/<slug>/workflow_runs/<run_id>.json
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .. import paths


def _def_path(wf_id: str) -> Path:
    return paths.workflows_dir() / f"{wf_id}.json"


def _read_json(p: Path, default: Any) -> Any:
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text() or json.dumps(default))
    except json.JSONDecodeError:
        return default


def _write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


# ---- definitions ----
def list_defs() -> list[dict[str, Any]]:
    paths.workflows_dir().mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(paths.workflows_dir().glob("*.json")):
        d = _read_json(p, None)
        if isinstance(d, dict):
            out.append(d)
    return out


def get_def(wf_id: str) -> dict[str, Any] | None:
    d = _read_json(_def_path(wf_id), None)
    return d if isinstance(d, dict) else None


def create_def(data: dict[str, Any]) -> dict[str, Any]:
    wf = {
        "id": data.get("id") or _new_id(),
        "name": data.get("name") or "Untitled workflow",
        "description": data.get("description") or "",
        "steps": [
            {"id": s.get("id") or _new_id(), "title": s.get("title", ""), "agent_id": s.get("agent_id")}
            for s in (data.get("steps") or [])
        ],
    }
    _write_json(_def_path(wf["id"]), wf)
    return wf


def update_def(wf_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    wf = get_def(wf_id)
    if not wf:
        return None
    wf.update({k: v for k, v in data.items() if k != "id" and v is not None})
    _write_json(_def_path(wf_id), wf)
    return wf


def delete_def(wf_id: str) -> bool:
    p = _def_path(wf_id)
    if p.exists():
        p.unlink()
        return True
    return False


# ---- runs (global; single-project product) ----
def _runs_dir() -> Path:
    return paths.home() / "workflow_runs"


def _run_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.json"


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


def create_run(wf: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
    now = time.time()
    run = {
        "id": _new_id(),
        "workflow_id": wf["id"],
        "name": wf.get("name", ""),
        "session_id": session_id,
        "status": "running",
        "steps": [
            {"id": s["id"], "title": s.get("title", ""), "status": "pending", "output": ""}
            for s in wf.get("steps", [])
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
    for wf in _SEED:
        if not _def_path(wf["id"]).exists():
            create_def(wf)
