"""Persistent token-usage telemetry (usage-stats-design.md §4/§5).

Every LLM call appends ONE line to an append-only per-day JSONL log under
``~/.ginno/usage/requests-YYYY-MM-DD.jsonl`` (local-time dates). These logs
are the single source of truth for the Settings → 用量统计 page: overview
trends, per-session aggregates, and the request log all stream-aggregate them
on demand. No precomputed tables — a heavy day is ~hundreds of KB, and
completed past days are cached in-process keyed by (path, mtime, size).

Design invariants:

* Recording NEVER blocks or breaks the conversation: every public entry point
  swallows its own errors (log-and-continue).
* Privacy: only counters and metadata are stored — never prompt/response
  content (design §4.4).
* Retention: logs older than ``RETENTION_DAYS`` are deleted at startup and
  opportunistically on the first write of a new day (design §4.3).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from .usage import cache_hit_ratio

_log = logging.getLogger("ginno.usage")

RETENTION_DAYS = 90
_FILE_PREFIX = "requests-"
_SOURCES = {"chat", "goal", "compaction", "workflow", "memory", "kb", "probe", "other"}

# Parsed COMPLETED days are immutable, so cache them. Today's file is always
# re-read (it keeps growing). Key: (path, mtime_ns, size).
_DAY_CACHE: dict[tuple, list[dict]] = {}
_LAST_CLEANUP_DAY: str | None = None


def reset_cache() -> None:
    """Drop in-process caches (tests switch $GINNO_HOME between cases)."""
    global _LAST_CLEANUP_DAY
    _DAY_CACHE.clear()
    _LAST_CLEANUP_DAY = None


def _date_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_path(date_str: str) -> Path:
    return paths.usage_dir() / f"{_FILE_PREFIX}{date_str}.jsonl"


def _today() -> str:
    return _date_str(time.time())


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #
def record(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    provider: str,
    model: str,
    source: str = "chat",
    session_id: str | None = None,
    project_slug: str | None = None,
    agent_id: str | None = None,
    turn_id: str | None = None,
    latency_ms: int | None = None,
    ok: bool = True,
    error: str | None = None,
    ts: float | None = None,
) -> None:
    """Append one LLM-call record. Token fields must already be normalized
    (whole-prompt input; see usage.py). Never raises."""
    try:
        ts = float(ts if ts is not None else time.time())
        entry = {
            "ts": ts,
            "session_id": session_id,
            "project_slug": project_slug,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "source": source if source in _SOURCES else "other",
            "provider": provider or "",
            "model": model or "",
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_read_tokens": int(cache_read_tokens),
            "cache_creation_tokens": int(cache_creation_tokens),
            "latency_ms": latency_ms,
            "ok": bool(ok),
            "error": (error or None) if not ok else None,
        }
        day = _date_str(ts)
        paths.usage_dir().mkdir(parents=True, exist_ok=True)
        with open(_day_path(day), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _maybe_cleanup(day)
    except Exception:  # noqa: BLE001 — telemetry must never break a turn
        _log.warning("usage record failed", exc_info=True)


def _maybe_cleanup(today: str) -> None:
    """Cleanup at most once per day, piggy-backed on the first write."""
    global _LAST_CLEANUP_DAY
    if _LAST_CLEANUP_DAY == today:
        return
    _LAST_CLEANUP_DAY = today
    try:
        cleanup(RETENTION_DAYS)
    except Exception:  # noqa: BLE001
        _log.warning("usage cleanup failed", exc_info=True)


def cleanup(retention_days: int = RETENTION_DAYS) -> int:
    """Delete per-day logs older than ``retention_days``. Returns count."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    removed = 0
    udir = paths.usage_dir()
    if not udir.is_dir():
        return 0
    for p in udir.glob(f"{_FILE_PREFIX}*.jsonl"):
        day = p.name[len(_FILE_PREFIX):-len(".jsonl")]
        if day < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------- #
# Read / iterate
# --------------------------------------------------------------------------- #
def load_day(date_str: str) -> list[dict]:
    """All records of one day (empty when the file is absent). Corrupt lines
    are skipped, never fatal. Completed days are cached in-process."""
    p = _day_path(date_str)
    if not p.exists():
        return []
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size)
    cached = _DAY_CACHE.get(key)
    if cached is not None:
        return cached
    entries: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(e, dict):
                    entries.append(e)
    except OSError:
        return []
    if date_str != _today():
        # bounded cache: drop oldest when it grows beyond ~120 days of data
        if len(_DAY_CACHE) > 130:
            _DAY_CACHE.pop(next(iter(_DAY_CACHE)))
        _DAY_CACHE[key] = entries
    return entries


def _parse_date(s: str | None, default: str) -> str:
    if not s:
        return default
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return default


def iter_range(from_date: str, to_date: str):
    """Yield records for each day in [from_date, to_date] (inclusive)."""
    d = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        yield from load_day(ds)
        d += timedelta(days=1)


def _acc() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "calls": 0,
    }


def _add(acc: dict[str, int], e: dict) -> None:
    acc["input_tokens"] += int(e.get("input_tokens") or 0)
    acc["output_tokens"] += int(e.get("output_tokens") or 0)
    acc["cache_read_tokens"] += int(e.get("cache_read_tokens") or 0)
    acc["cache_creation_tokens"] += int(e.get("cache_creation_tokens") or 0)
    acc["calls"] += 1


def _with_ratio(acc: dict[str, int]) -> dict:
    return {**acc, "cache_hit_ratio": cache_hit_ratio(acc)}


# --------------------------------------------------------------------------- #
# Aggregates (design §5)
# --------------------------------------------------------------------------- #
def aggregate_overview(days: int) -> dict:
    """KPI + daily series + provider/model/source breakdown for the trailing window."""
    today = datetime.now()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    daily: list[dict] = []
    totals = _acc()
    providers: dict[str, dict] = {}
    models: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    sessions_seen: set[str] = set()
    for ds in dates:
        day_acc = _acc()
        for e in load_day(ds):
            _add(day_acc, e)
            _add(totals, e)
            p = e.get("provider") or "?"
            pa = providers.setdefault(p, _acc())
            _add(pa, e)
            mkey = f"{p}/{e.get('model') or '?'}"
            ma = models.setdefault(mkey, _acc())
            _add(ma, e)
            ma["provider"] = p  # type: ignore[assignment]
            ma["model"] = e.get("model") or "?"  # type: ignore[assignment]
            # source split (design §3.6): totals stay whole-account; the
            # breakdown answers chat vs workflow vs background work.
            sa = sources.setdefault(e.get("source") or "other", _acc())
            _add(sa, e)
            if e.get("session_id"):
                sessions_seen.add(e["session_id"])
        daily.append({"date": ds, **_with_ratio(day_acc)})
    today_str = dates[-1]
    today_acc = _acc()
    for e in load_day(today_str):
        _add(today_acc, e)
    return {
        "window": {"days": days, "from": dates[0], "to": dates[-1]},
        "today": _with_ratio(today_acc),
        "totals": _with_ratio(totals),
        "sessions_active": len(sessions_seen),
        "daily": daily,
        "providers": [
            {"provider": p, **_with_ratio(a)}
            for p, a in sorted(providers.items(), key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"]))
        ],
        "models": [
            _with_ratio(a)
            for _, a in sorted(models.items(), key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"]))
        ],
        "sources": [
            {"source": s, **_with_ratio(a)}
            for s, a in sorted(sources.items(), key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"]))
        ],
    }


def aggregate_hourly(date_str: str | None = None) -> dict:
    ds = _parse_date(date_str, _today())
    hours = [{
        "hour": h, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "calls": 0,
    } for h in range(24)]
    for e in load_day(ds):
        h = int(time.localtime(float(e.get("ts") or 0)).tm_hour)
        b = hours[h]
        b["input_tokens"] += int(e.get("input_tokens") or 0)
        b["output_tokens"] += int(e.get("output_tokens") or 0)
        b["cache_read_tokens"] += int(e.get("cache_read_tokens") or 0)
        b["calls"] += 1
    return {"date": ds, "hours": hours}


def aggregate_sessions(from_date: str | None, to_date: str | None, sort: str = "total", limit: int = 200) -> list[dict]:
    frm = _parse_date(from_date, _date_str(time.time() - 86400 * (RETENTION_DAYS - 1)))
    to = _parse_date(to_date, _today())
    by_session: dict[str, dict] = {}
    for e in iter_range(frm, to):
        sid = e.get("session_id") or ""
        s = by_session.setdefault(sid, _acc() | {"last_ts": 0.0, "project_slug": e.get("project_slug"), "agent_id": e.get("agent_id")})
        _add(s, e)
        s["last_ts"] = max(s["last_ts"], float(e.get("ts") or 0))
    rows = []
    for sid, s in by_session.items():
        rows.append({
            "session_id": sid,
            "project_slug": s.get("project_slug"),
            "agent_id": s.get("agent_id"),
            "last_active": s["last_ts"],
            **_with_ratio({k: s[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "calls")}),
        })
    keymap = {
        "total": lambda r: r["input_tokens"] + r["output_tokens"],
        "input": lambda r: r["input_tokens"],
        "output": lambda r: r["output_tokens"],
        "calls": lambda r: r["calls"],
        "hit": lambda r: r["cache_hit_ratio"],
        "updated": lambda r: r["last_active"],
    }
    rows.sort(key=keymap.get(sort, keymap["total"]), reverse=True)
    return rows[: max(1, min(limit, 500))]


def session_totals(session_id: str, from_date: str | None = None, to_date: str | None = None) -> dict | None:
    """Aggregate one session's recorded usage; None when nothing was logged."""
    frm = _parse_date(from_date, _date_str(time.time() - 86400 * RETENTION_DAYS))
    to = _parse_date(to_date, _today())
    acc = _acc()
    found = False
    for e in iter_range(frm, to):
        if e.get("session_id") == session_id:
            _add(acc, e)
            found = True
    return _with_ratio(acc) if found else None


def query_requests(
    date_str: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    ds = _parse_date(date_str, _today())
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = []
    for e in load_day(ds):
        if provider and e.get("provider") != provider:
            continue
        if model and e.get("model") != model:
            continue
        if source and e.get("source") != source:
            continue
        if session_id and e.get("session_id") != session_id:
            continue
        rows.append(e)
    rows.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    total = len(rows)
    start = (page - 1) * page_size
    return {"date": ds, "total": total, "page": page, "page_size": page_size, "rows": rows[start:start + page_size]}
