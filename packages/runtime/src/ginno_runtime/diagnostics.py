"""Built-in diagnostics (P4-1 / P4-2).

The external harness (``app-iter-harness/lib/ux.py``) proved the checks are
worth having; this module brings the read-only ones into the app so the user
can answer "is anything broken?" without the repo. Two entry points:

* :func:`run_diagnostics` — the full report behind ``GET /api/diagnostics``
  (settings → 诊断).
* :func:`startup_check` — a light subset run once in the lifespan; its
  warning count rides ``/api/health`` so the shell can show a badge.

Everything here is read-only and best-effort: a broken store yields a finding,
never an exception.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter

from . import artifacts as art_store
from . import paths
from . import schedule_store
from . import server_shared as shared
from .session_meta import _find_meta, _session_meta_list

_log = logging.getLogger("ginno.diagnostics")

STALE_ERROR_DAYS = 7
ARCHIVE_EMPTY_DAYS = 1

# Filled by startup_check(); read by /api/health.
LAST_STARTUP: dict = {"at": None, "warnings": 0, "items": []}


def _project_slugs() -> list[str]:
    root = paths.home() / "projects"
    try:
        return sorted(
            p.name for p in root.iterdir() if (p / "sessions" / "_index.json").exists()
        )
    except OSError:
        return []


def _valid_session_ids() -> set[str]:
    ids: set[str] = set()
    for slug in _project_slugs():
        ids.update(str(m.get("id")) for m in _session_meta_list(slug) if m.get("id"))
    return ids


def _check_data() -> list[dict]:
    """Cross-store integrity: dangling refs, orphan assets, stale state."""
    from .maintenance import session_reference_map

    issues: list[dict] = []
    sessions = _valid_session_ids()
    refs = session_reference_map()
    dangling = {sid: where for sid, where in refs.items() if sid not in sessions}
    if dangling:
        issues.append(
            {
                "id": "data.dangling_session_refs",
                "severity": "high",
                "count": len(dangling),
                "detail": ", ".join(f"{sid[:8]}←{refs[sid][0]}" for sid in list(dangling)[:5]),
            }
        )

    orphan_assets: list[dict] = []
    for slug in _project_slugs():
        for a in art_store.list_artifacts(slug):
            sid = a.get("session_id")
            if sid and str(sid) not in sessions:
                orphan_assets.append(a)
                continue
            ref = str(a.get("ref") or "")
            if a.get("kind") in ("file", "image", "doc") and (
                ref.startswith("/") or ref.startswith("~")
            ) and not os.path.exists(os.path.expanduser(ref)):
                orphan_assets.append(a)
    if orphan_assets:
        issues.append(
            {
                "id": "data.orphan_assets",
                "severity": "medium",
                "count": len(orphan_assets),
                "detail": ", ".join(str(a.get("name"))[:24] for a in orphan_assets[:5]),
            }
        )

    now = time.time()
    stale_errors = 0
    dup_titles: dict[str, int] = {}
    archived = 0
    for slug in _project_slugs():
        rows = _session_meta_list(slug)
        stale_errors += sum(
            1
            for m in rows
            if m.get("last_error")
            and now - float(m.get("updated") or 0) > STALE_ERROR_DAYS * 86400
        )
        archived += sum(1 for m in rows if m.get("archived"))
        counts = Counter(str(m.get("title") or "").strip() for m in rows)
        for title, n in counts.items():
            if title and n > 2:
                dup_titles[title] = n
    if stale_errors:
        issues.append(
            {
                "id": "data.stale_errors",
                "severity": "medium",
                "count": stale_errors,
                "detail": "错误状态超过 7 天未更新",
            }
        )
    if dup_titles:
        issues.append(
            {
                "id": "data.duplicate_titles",
                "severity": "low",
                "count": len(dup_titles),
                "detail": "; ".join(f"{n}x {t[:18]}" for t, n in list(dup_titles.items())[:4]),
            }
        )
    if archived:
        issues.append(
            {
                "id": "data.archived_sessions",
                "severity": "info",
                "count": archived,
                "detail": "已归档（提示，不是问题）",
            }
        )
    return issues


def _check_tasks() -> dict:
    rows = schedule_store.query_runs(page=1, page_size=200).get("rows") or []
    terminal = [r for r in rows if r.get("status") in ("ok", "error", "missed", "caught_up")]
    fails = [r for r in terminal if r.get("status") in ("error", "missed")]
    latest_fail = next((r for r in terminal if r.get("status") == "error"), None)
    issues: list[dict] = []
    if terminal and len(fails) / len(terminal) > 0.3:
        issues.append(
            {
                "id": "tasks.failure_rate",
                "severity": "medium",
                "count": len(fails),
                "detail": f"{len(fails)}/{len(terminal)} 次失败（历史累计；补跑已开启）",
            }
        )
    return {
        "total": len(terminal),
        "ok": sum(1 for r in terminal if r.get("status") == "ok"),
        "failed": len(fails),
        "caught_up": sum(1 for r in terminal if r.get("status") == "caught_up"),
        "latest_failure": (
            {
                "run_id": latest_fail.get("run_id"),
                "task": latest_fail.get("task_name"),
                "error": latest_fail.get("error"),
                "hint": latest_fail.get("error_hint"),
                "kind": latest_fail.get("error_kind"),
            }
            if latest_fail
            else None
        ),
        "issues": issues,
    }


def _check_runtime() -> dict:
    from . import runtime_watch

    status = runtime_watch.status()
    issues: list[dict] = []
    if status.get("runtime_stale"):
        issues.append(
            {
                "id": "runtime.stale_bundle",
                "severity": "high",
                "count": 1,
                "detail": "运行时二进制在启动后被替换，重启 Ginno 后生效",
            }
        )
    sessions = getattr(shared, "_SESSIONS", {}) or {}
    return {
        "frozen": bool(status.get("bundle_mtime")),
        "runtime_stale": bool(status.get("runtime_stale")),
        "uptime_s": round(time.time() - float(status.get("process_started") or time.time()), 1),
        "loaded_sessions": len(sessions),
        "issues": issues,
    }


def _check_contracts() -> dict:
    """Route coverage for the GETs the frontend relies on (API-03 class bugs)."""
    from .server import app

    # This FastAPI build keeps included routers lazy (``_IncludedRouter``), so
    # ``app.routes`` has no per-endpoint paths — the OpenAPI schema does.
    try:
        have = set((app.openapi() or {}).get("paths") or {})
    except Exception:  # noqa: BLE001 — schema build failure is itself a finding
        have = set()
    expected = (
        "/api/sessions/search",
        "/api/sessions/bulk",
        "/api/artifacts/{artifact_id}",
        "/api/folders/{folder_id}",
        "/api/agents/{agent_id}",
        "/api/diagnostics",
    )
    missing = [p for p in expected if p not in have]
    issues = (
        [
            {
                "id": "contract.missing_routes",
                "severity": "medium",
                "count": len(missing),
                "detail": ", ".join(missing),
            }
        ]
        if missing
        else []
    )
    return {"missing_routes": missing, "issues": issues}


def run_diagnostics() -> dict:
    sections = {
        "data": {"issues": _check_data()},
        "tasks": _check_tasks(),
        "runtime": _check_runtime(),
        "contracts": _check_contracts(),
    }
    issues = [i for s in sections.values() for i in (s.get("issues") or [])]
    return {
        "ok": True,
        "generated_at": time.time(),
        "warnings": sum(1 for i in issues if i.get("severity") in ("high", "medium")),
        "issues": issues,
        "sections": sections,
    }


def startup_check() -> None:
    """Light subset for the lifespan: no checkpoint reads, no HTTP."""
    try:
        issues = _check_data()
        runtime = _check_runtime()
        issues += runtime.get("issues") or []
        warnings = [i for i in issues if i.get("severity") in ("high", "medium")]
        LAST_STARTUP.update({"at": time.time(), "warnings": len(warnings), "items": issues})
        if warnings:
            _log.warning(
                "startup_check warnings=%d: %s",
                len(warnings),
                "; ".join(str(i.get("id")) for i in warnings),
            )
    except Exception:  # noqa: BLE001 — self-check must never break startup
        _log.exception("startup_check_failed")


def startup_summary() -> dict:
    return {
        "diagnostics_at": LAST_STARTUP.get("at"),
        "diagnostics_warnings": int(LAST_STARTUP.get("warnings") or 0),
    }
