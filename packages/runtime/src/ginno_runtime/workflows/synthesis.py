"""Synthesis-case recording for the session→workflow summarizer
(master-plan §3.1). Each summarize attempt builds a case directory::

    ~/.ginno/synthesis/<yyyymmdd-HHMMSS>-<session8>/
        input.json      # replayable snapshot: trace + model + prompt version
        attempts.jsonl  # one line per LLM attempt (raw output, parse, errors)
        output.json     # final DSL + fail_stage label + latency
        outcome.json    # async backfill: adopted / edited / first_run / feedback

Everything is best-effort (never raises into the summarize hot path) and local.
Retention is bounded lazily by :func:`prune_cases`.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import paths

MAX_CASES = 200
MAX_AGE_DAYS = 90


def _root() -> Path:
    p = paths.home() / "synthesis"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def new_case(
    session_id: str,
    *,
    provider: str,
    model: str,
    last_n: int | None,
    trace: str,
    session_stats: dict,
    prompt_version: str,
) -> tuple[Path, str] | tuple[None, None]:
    """Create a fresh case directory and write input.json. Returns
    (case_dir, synthesis_id) or (None, None) on any failure."""
    try:
        ts = time.time()
        stamp = datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")
        synthesis_id = f"{stamp}-{(session_id or '')[:8]}"
        case_dir = _root() / synthesis_id
        case_dir.mkdir(parents=True, exist_ok=True)
        _write(
            case_dir / "input.json",
            {
                "synthesis_id": synthesis_id,
                "session_id": session_id,
                "ts": ts,
                "prompt_version": prompt_version,
                "provider": provider,
                "model": model,
                "last_n": last_n,
                "session_stats": session_stats,
                "trace": trace,
            },
        )
        return case_dir, synthesis_id
    except Exception:
        return None, None


def record_attempt(
    case_dir: Path | None,
    *,
    attempt: int,
    latency_ms: int,
    raw: str,
    parse: str,
    validate_errors: list[str],
    hint_fed_back: str | None,
) -> None:
    if not case_dir:
        return
    try:
        line = {
            "attempt": attempt,
            "latency_ms": latency_ms,
            "raw": raw,
            "parse": parse,
            "validate_errors": validate_errors,
            "hint_fed_back": hint_fed_back,
        }
        with (case_dir / "attempts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def finish_case(
    case_dir: Path | None,
    *,
    status: str,
    dsl: dict | None,
    fail_stage: str | None,
    total_latency_ms: int,
    attempts_used: int,
) -> None:
    if not case_dir:
        return
    try:
        _write(
            case_dir / "output.json",
            {
                "status": status,
                "fail_stage": fail_stage,
                "dsl": dsl,
                "total_latency_ms": total_latency_ms,
                "attempts_used": attempts_used,
                "finished_ts": time.time(),
            },
        )
    except Exception:
        pass


def backfill_outcome(case_dir: Path | None, **fields: Any) -> None:
    """Merge fields into outcome.json (idempotent; missing file is created)."""
    if not case_dir:
        return
    try:
        p = case_dir / "outcome.json"
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
        cur.update({k: v for k, v in fields.items() if v is not None})
        _write(p, cur)
    except Exception:
        pass


def find_case_by_synthesis_id(synthesis_id: str) -> Path | None:
    p = _root() / synthesis_id
    return p if p.is_dir() else None


def case_for_workflow(workflow_id: str) -> Path | None:
    """Locate the case whose outcome.json already references this workflow, or
    whose output DSL matches. Used to backfill first_run results."""
    try:
        for case_dir in sorted(_root().iterdir(), reverse=True):
            if not case_dir.is_dir():
                continue
            oc = case_dir / "outcome.json"
            if oc.exists():
                try:
                    if json.loads(oc.read_text(encoding="utf-8")).get("workflow_id") == workflow_id:
                        return case_dir
                except Exception:
                    continue
        return None
    except Exception:
        return None


def load_case(synthesis_id: str) -> dict | None:
    case_dir = find_case_by_synthesis_id(synthesis_id)
    if not case_dir:
        return None
    out: dict = {"synthesis_id": synthesis_id}
    for name in ("input.json", "output.json", "outcome.json"):
        f = case_dir / name
        if f.exists():
            try:
                out[name.split(".")[0]] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                out[name.split(".")[0]] = None
    att = case_dir / "attempts.jsonl"
    if att.exists():
        try:
            out["attempts"] = [
                json.loads(l) for l in att.read_text(encoding="utf-8").splitlines() if l.strip()
            ]
        except Exception:
            out["attempts"] = []
    return out


def list_cases(limit: int = 100) -> list[dict]:
    """Return lightweight summaries (no trace/raw), newest first."""
    try:
        cases = []
        for case_dir in sorted(_root().iterdir(), reverse=True):
            if not case_dir.is_dir():
                continue
            row: dict = {"synthesis_id": case_dir.name}
            inp = case_dir / "input.json"
            outp = case_dir / "output.json"
            oc = case_dir / "outcome.json"
            if inp.exists():
                try:
                    d = json.loads(inp.read_text(encoding="utf-8"))
                    row["ts"] = d.get("ts")
                    row["prompt_version"] = d.get("prompt_version")
                    row["session_stats"] = d.get("session_stats")
                except Exception:
                    pass
            if outp.exists():
                try:
                    d = json.loads(outp.read_text(encoding="utf-8"))
                    row["status"] = d.get("status")
                    row["fail_stage"] = d.get("fail_stage")
                    row["attempts_used"] = d.get("attempts_used")
                except Exception:
                    pass
            if oc.exists():
                try:
                    row["outcome"] = json.loads(oc.read_text(encoding="utf-8"))
                except Exception:
                    pass
            cases.append(row)
            if len(cases) >= limit:
                break
        return cases
    except Exception:
        return []


def prune_cases() -> None:
    """Enforce retention bounds (best-effort, lazy)."""
    try:
        dirs = [d for d in _root().iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.name, reverse=True)  # newest first by name
        cutoff = time.time() - MAX_AGE_DAYS * 86400
        for i, d in enumerate(dirs):
            too_old = False
            inp = d / "input.json"
            if inp.exists():
                try:
                    too_old = json.loads(inp.read_text(encoding="utf-8")).get("ts", 0) < cutoff
                except Exception:
                    too_old = False
            if i >= MAX_CASES or too_old:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
