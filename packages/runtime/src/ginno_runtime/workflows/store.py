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
from . import expr as wf_expr


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


def is_system_def(wf_id: str) -> bool:
    """Built-in (seeded) workflow defs are protected from deletion but stay
    listed like any other workflow."""
    return bool((_read_json(_meta_path(wf_id), None) or {}).get("system"))


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
        "system": bool((meta or {}).get("system")),
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
    # A caller-supplied id flows straight into file paths below; reject anything
    # that isn't a flat slug so "../" can't escape the workflows dir (path
    # traversal → arbitrary file/dir write).
    if data.get("id") and not wf_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("workflow id must match [A-Za-z0-9_-]+")
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
    meta = {
        "id": wf_id,
        "name": d["name"],
        "description": d["description"],
        "current": 1,
        "versions": [1],
        "system": bool(data.get("system")),
    }
    # Provenance link back to the synthesis case that produced this workflow
    # (quality-plan §3.1 outcome backfill).
    if data.get("synthesis_id"):
        meta["synthesized_from"] = {"synthesis_id": data["synthesis_id"]}
    _write_json(_meta_path(wf_id), meta)
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
    out: list[dict[str, Any]] = []
    for n in sorted(meta.get("versions") or []):
        row: dict[str, Any] = {"version": n, "current": n == cur}
        # Version files are immutable append-only snapshots: mtime == commit time
        # (used by the version-history drawer's relative timestamps).
        try:
            f = _version_path(wf_id, n)
            if f.exists():
                row["ts"] = f.stat().st_mtime
        except OSError:
            pass
        out.append(row)
    return out


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
# Terminal statuses (a run in one of these has no live task and will never
# progress on its own; retry creates a NEW run). "paused" is deliberately NOT
# terminal — it is resumable from its checkpoint.
TERMINAL_STATUSES = ("done", "failed", "cancelled", "interrupted")


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


def create_run(
    wf: dict[str, Any],
    session_id: str | None = None,
    present_in_session_id: str | None = None,
    context_override: dict | None = None,
    retried_from: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    d, ver = _wf_dsl_and_version(wf)
    # include_extracts: the run's step list must account for the compiler's
    # injected ``<id>__extract`` nodes, otherwise the step-based run-status
    # recomputation finalizes the run before extraction runs (master-plan §2.2).
    steps = wf_dsl.steps_from_dsl(d, include_extracts=True)
    # Fill the {{placeholders}} in step titles with the run's initial context
    # (DSL context.initial merged with the trigger's override — exactly what the
    # engine seeds) so the run reads as real content ("把 dingtalk 上 id=123 的
    # 待办「写周报」标记为已完成…") instead of the raw template. Placeholders that
    # only resolve at runtime (earlier steps' outputs, loop vars) are left as-is.
    initial = dict((d.get("context") or {}).get("initial") or {})
    if isinstance(context_override, dict):
        initial.update(context_override)
    if initial:
        for s in steps:
            t = s.get("title")
            if isinstance(t, str) and "{{" in t:
                s["title"] = wf_expr.render_partial(t, initial)
    run = {
        "id": _new_id(),
        "workflow_id": wf.get("id", ""),
        "name": wf.get("name", ""),
        "session_id": session_id,
        # where the run block should render in-chat (design A: run 回到对话)
        "present_in_session_id": present_in_session_id or session_id,
        "dsl_version": ver,
        "status": "running",
        # Persist the trigger's context override so a retry can re-run with the
        # exact same inputs (previously discarded → retries lost the provider/
        # skill/template vars of todo-sync runs). Also useful for debugging
        # ("what did this run execute with?").
        "context_override": context_override,
        # Retry provenance: which run this one re-executes (None on first run).
        "retried_from": retried_from,
        # Last failure reason (surfaced by the UI without parsing events.jsonl);
        # cleared when the run reaches done.
        "error": None,
        # Structured companion of ``error`` for localization: {node_id, traceback}.
        # Optional on legacy runs (files written before this field existed).
        "error_detail": None,
        # Wall-clock end; set when the run reaches any terminal status.
        "finished": None,
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
    for s in run.get("steps", []):
        if s["id"] == step_id:
            s["status"] = status
            if output:
                s["output"] = output
    # Only a run that is still "running" may have its overall status
    # recomputed from its steps. A late step update must never demote a
    # terminal status (failed/cancelled/interrupted) or a paused run back to
    # done/running.
    if run.get("status") == "running":
        # "skipped" is terminal too: a loop body skipped via on_empty must not
        # keep the run "running" forever (master-plan §2.1).
        done = all(s["status"] in ("done", "failed", "skipped") for s in run.get("steps", []))
        run["status"] = "done" if done else "running"
        if done and not run.get("finished"):
            run["finished"] = time.time()
    run["updated"] = time.time()
    _write_json(_run_path(run_id), run)
    return run


def delete_run(run_id: str) -> bool:
    """Remove a run's persisted artifacts: the run JSON and its events JSONL.

    Both live in the same ``workflow_runs`` dir. Checkpoint files (under the
    project sessions dir) are removed by the caller (server), which owns the
    ``project_slug`` knowledge. Returns True if any artifact existed.
    """
    from . import events as wf_events

    p = _run_path(run_id)
    existed = p.exists()
    p.unlink(missing_ok=True)
    ev_existed = wf_events.delete_events(run_id)
    return existed or ev_existed


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
    },
    # Generic TODO-provider sync pair (todo-provider design). `skills` is a
    # {{template}} resolved per run from context_override ({skill, provider,
    # ext_id, title, url}) so ONE definition serves every platform.
    {
        "id": "todo-push",
        "name": "🔄 TODO 回同步",
        "description": "把本地完成的 TODO 同步回 ext ref 指向的外部 TODO 平台。",
        "system": True,
        "dsl": {
            "entry": "push",
            "nodes": [
                {
                    "id": "push",
                    "type": "agent",
                    "agent": "dev",
                    "skills": ["{{skill}}"],
                    # Short human label for the run panel (templated, rendered per
                    # run); the verbose `goal` below is what the agent executes.
                    "title": "把待办「{{title}}」标记为完成（{{provider}}）",
                    "goal": (
                        "把外部 TODO 平台 {{provider}} 上 id={{ext_id}} 的待办「{{title}}」标记为已完成。"
                        "优先使用注入的 skill；若没有注入 skill，则直接调用 {{mcp}} 服务的 MCP 工具"
                        "（工具名以 mcp_{{mcp}}_ 开头，先列出可用工具再选）。"
                        "若它不存在或已完成则直接说明。完成后简要回复结果。"
                    ),
                }
            ],
            "edges": [],
        },
    },
    {
        "id": "todo-pull",
        "name": "🔄 TODO 拉取",
        "description": "拉取外部 TODO 平台未完成待办，镜像到本地 Daily TODO（带 ext ref）。",
        "system": True,
        "dsl": {
            "entry": "pull",
            "nodes": [
                {
                    "id": "pull",
                    "type": "agent",
                    "agent": "dev",
                    "skills": ["{{skill}}"],
                    # Short human label for the run panel (templated, rendered per
                    # run); the verbose `goal` below is what the agent executes.
                    "title": "拉取 {{provider}} 的未完成待办",
                    "goal": (
                        "拉取我在外部 TODO 平台 {{provider}} 上【未完成】的待办。"
                        "调用列表工具时参数必须严格为："
                        '{"pageNum": "1", "pageSize": "20", "roleTypes": ["creator", "executor"], '
                        '"todoStatus": "false"}（roleTypes 必填且是数组、pageSize 用 20——该网关对 50 返回空；缺字段会报错）。'
                        "对每一条：先 todo_list 查本地；若已存在相同 ext（provider={{provider}} 且 id 相同）"
                        "或标题相同的条目，则用 todo_update 同步标题/截止时间并补上 ext；"
                        "否则 todo_create 创建，且必须传 "
                        'ext=[{"provider": "{{provider}}", "id": "<平台待办id>"}]'
                        "（id 只放 ext，禁止塞进 category；ext 是唯一关联字段）。"
                        "不要创建重复条目；最后 todo_list 确认。"
                    ),
                }
            ],
            "edges": [],
        },
    },
]


def ensure_seeded() -> None:
    paths.workflows_dir().mkdir(parents=True, exist_ok=True)
    # migrate any legacy seed file first
    for wf in _SEED:
        _migrate_if_needed(wf["id"])
    for wf in _SEED:
        if not _is_new_layout(wf["id"]):
            create_def(wf)
        elif wf.get("system") and isinstance(wf.get("dsl"), dict):
            # System seeds track the shipped definition: push a new immutable
            # version when stored nodes drift (e.g. goal wording upgraded).
            cur = get_def(wf["id"])
            seed_d = wf_dsl.normalize_dsl(dict(wf["dsl"]))
            seed_d["name"] = wf.get("name") or seed_d.get("name") or "Untitled workflow"
            seed_d["description"] = wf.get("description") or seed_d.get("description") or ""
            if cur and (cur["dsl"].get("nodes") or []) != (seed_d.get("nodes") or []):
                try:
                    _write_version(wf["id"], seed_d, commit="system seed update")
                except Exception:
                    pass
            # existing installs: stamp the protection flag onto old metas
            meta = _read_json(_meta_path(wf["id"]), None)
            if isinstance(meta, dict) and not meta.get("system"):
                meta["system"] = True
                _write_json(_meta_path(wf["id"]), meta)
