"""Meta / tree helpers shared by every lifecycle path: subagent meta
predicates, the process-wide running scan, the concurrency cap (setting + env
override), soft-budget warnings, and the listing rows behind
list_subagents / wait_for_subagents."""

from __future__ import annotations

import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


def max_concurrent() -> int:
    """The live concurrency cap: settings ``subagent.max_concurrent`` (1-16),
    overridden by ``GINNO_SUBAGENT_MAX`` (highest priority — tests). Read on
    every call so a settings write takes effect without a restart."""
    raw: str | None = _NS.os.environ.get("GINNO_SUBAGENT_MAX")
    if raw and raw.strip():
        try:
            return _clamp_concurrent(int(raw.strip()))
        except ValueError:
            _NS._log.warning("subagent_max_env_invalid value=%r", raw)
    try:
        p = _NS.paths.settings_path()
        if p.exists():
            from ginno_runtime import jsonx as _json

            settings = _json.loads(p.read_text() or "{}")
            val = ((settings.get("subagent") or {}) if isinstance(settings, dict) else {}).get(
                "max_concurrent"
            )
            if val is not None:
                return _clamp_concurrent(int(val))
    except (OSError, ValueError, TypeError):
        _NS._log.info("subagent_max_settings_unreadable", exc_info=True)
    return _NS.SUBAGENT_MAX_CONCURRENT


def _clamp_concurrent(value: int) -> int:
    return max(
        _NS.SUBAGENT_MIN_CONCURRENT, min(_NS.SUBAGENT_MAX_CONCURRENT_LIMIT, int(value))
    )


# 报告回传策略缺省值（2026-10-30 用户定夺：报告不该压缩——压缩曾在网关卡
# 9 分钟阻塞 finalize/上报；超大报告落文件让父会话按文件读）。
REPORT_COMPRESS_DEFAULT = False
REPORT_INLINE_MAX_DEFAULT = 12000
REPORT_INLINE_MAX_LIMIT = 1_000_000


def report_settings() -> tuple[bool, int]:
    """(compress_report, report_inline_max)：settings ``subagent.compress_report``
    （默认 False——不压缩，注入原文）与 ``subagent.report_inline_max``（默认
    12000 字，超过则全文落文件、注入指针+预览）。环境变量
    ``GINNO_SUBAGENT_REPORT_INLINE_MAX`` 优先（测试用）。每次调用都读设置，
    写设置即时生效（同 max_concurrent）。"""
    inline_max = REPORT_INLINE_MAX_DEFAULT
    raw: str | None = _NS.os.environ.get("GINNO_SUBAGENT_REPORT_INLINE_MAX")
    if raw and raw.strip():
        try:
            inline_max = max(1, min(REPORT_INLINE_MAX_LIMIT, int(raw.strip())))
        except ValueError:
            _NS._log.warning("subagent_report_inline_max_env_invalid value=%r", raw)
    compress = REPORT_COMPRESS_DEFAULT
    try:
        p = _NS.paths.settings_path()
        if p.exists():
            from ginno_runtime import jsonx as _json

            settings = _json.loads(p.read_text() or "{}")
            sub = (settings.get("subagent") or {}) if isinstance(settings, dict) else {}
            if sub.get("compress_report") is not None:
                compress = bool(sub.get("compress_report"))
            if sub.get("report_inline_max") is not None:
                inline_max = max(
                    1, min(REPORT_INLINE_MAX_LIMIT, int(sub.get("report_inline_max")))
                )
    except (OSError, ValueError, TypeError):
        _NS._log.info("subagent_report_settings_unreadable", exc_info=True)
    return compress, inline_max


def _is_subagent(meta: dict | None) -> bool:
    return bool(meta) and meta.get("type") == "subagent" and bool(meta.get("subagent"))


def _subagent_status(meta: dict | None) -> str:
    return str(((meta or {}).get("subagent") or {}).get("status") or "")


def _live_descendants(slug: str, session_id: str) -> list[dict]:
    return [
        m
        for m in _NS._session_meta_descendants(slug, session_id)
        if _subagent_status(m) in _NS.LIVE_STATUSES
    ]


def _running_subagent_metas() -> list[dict]:
    """Every RUNNING subagent across ALL projects (the cap is process-wide).

    The cap is about in-flight TURNS: a ``waiting`` parent holds no turn of
    its own (it is parked until a descendant wakes it), so it must not
    consume a concurrency slot — only ``running`` counts here. LIVE_STATUSES
    stays the completion gate's notion of "has unfinished work"."""
    out: list[dict] = []
    for idx in _NS.paths.home().glob("projects/*/sessions/_index.json"):
        from ..session_meta import _session_meta_list

        for m in _session_meta_list(idx.parent.parent.name):
            if _is_subagent(m) and _subagent_status(m) == "running":
                out.append(m)
    return out


def _touch_in_memory(session_id: str, patch: dict) -> None:
    s = _NS._SESSIONS.get(session_id)
    if s:
        s.update(patch)


async def _push_both(parent_id: str | None, child_id: str, event: str, data: dict) -> None:
    """subagent.* events go to the child's own sockets AND every ancestor's:
    a grandchild's status frame must reach the grandparent's sidebar too, or
    the nested status dots there go stale (the sidebar tree renders the whole
    parent_session_id chain, not just direct children)."""
    seen: set[str] = set()
    pid = parent_id
    while pid and pid not in seen:
        seen.add(pid)
        await _NS._push_session_event(pid, event, data)
        found = _NS._find_meta(pid)
        pid = found[0].get("parent_session_id") if found else None
    if child_id and child_id not in seen:
        await _NS._push_session_event(child_id, event, data)


# --------------------------------------------------------------------------- #
# soft-budget warnings (P3 contract 4) — notice event, once per session
# --------------------------------------------------------------------------- #

def _concurrency_warn_floor(cap: int) -> int:
    """running ≥ ceil(cap * 80%) fires the once-per-session warning."""
    import math

    return max(1, math.ceil(cap * _NS.WARN_RUNNING_RATIO))


async def _warn_once(session_id: str, seen: set[str], message: str) -> bool:
    if not session_id or session_id in seen:
        return False
    seen.add(session_id)
    try:
        await _NS._push_session_event(session_id, "notice", {"message": message})
    except Exception:
        _NS._log.exception("subagent_warn_push_failed session=%s", session_id)
        return True
    return True


async def _maybe_warn_concurrency(owner_session_id: str) -> bool:
    """Trigger 1: running count reached 80% of the (live) cap. Keyed by the
    OWNING session — each session is told once per runtime run."""
    cap = max_concurrent()
    running = len(_running_subagent_metas())
    floor = _concurrency_warn_floor(cap)
    if running < floor:
        return False
    # Ephemeral notice copy → inline bilingual t() (request-scoped locale
    # when spawned from a turn; settings locale in headless paths).
    return await _warn_once(
        owner_session_id,
        _NS._WARNED_CONCURRENCY,
        _NS.t(
            f"Heads-up: {running} subagents are now running concurrently"
            f" (80% of the cap of {cap}). Watch the split granularity; avoid mass delegation.",
            f"提示：当前已有 {running} 个子代理并发运行（达到上限 {cap} 的 80%）。"
            "请注意拆分粒度，避免大量委派。",
        ),
    )


# --------------------------------------------------------------------------- #
# stop cascade + listing + wait (contract 4/7)
# --------------------------------------------------------------------------- #

def _tree_ids(session_id: str, include_self: bool = True) -> list[str]:
    slug = _NS._session_slug(session_id)
    ids = [session_id] if include_self else []
    if slug:
        ids += [m["id"] for m in _NS._session_meta_descendants(slug, session_id)]
    return ids


def collect_subagent_rows(
    slug: str, session_id: str, include_descendants: bool = True
) -> list[dict]:
    """[{session_id, title, goal, status, depth, parent_session_id, elapsed_s}]
    for the direct children (and, optionally, all further descendants)."""
    rows: list[dict] = []

    def _row(m: dict) -> dict:
        sa = m.get("subagent") or {}
        return {
            "session_id": m["id"],
            "title": m.get("title") or "",
            "goal": str(sa.get("goal") or ""),
            "status": str(sa.get("status") or ""),
            "depth": int(m.get("depth") or 0),
            "parent_session_id": m.get("parent_session_id"),
            "elapsed_s": round(max(0.0, _NS.time.time() - float(m.get("created") or _NS.time.time())), 1),
        }

    from ..session_meta import _session_meta_children

    def _walk(pid: str) -> None:
        for m in _session_meta_children(slug, pid):
            rows.append(_row(m))
            if include_descendants:
                _walk(m["id"])

    _walk(session_id)
    return rows


def _wait_rows(rows: list[dict]) -> list[dict]:
    """Status summaries for the wait result — result_summary rides along once
    the child finished (that is the whole point of waiting)."""
    out: list[dict] = []
    for r in rows:
        item = {k: r[k] for k in ("session_id", "title", "goal", "status", "elapsed_s")}
        if r["status"] == "done":
            found = _NS._find_meta(r["session_id"])
            if found:
                item["result_summary"] = str(
                    (found[0].get("subagent") or {}).get("result_summary") or ""
                )
        out.append(item)
    return out
