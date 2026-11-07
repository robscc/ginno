"""Usage telemetry endpoints (usage-stats-design.md §5)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import usage_store
from ..lang import t
from ..server_shared import _SESSIONS, _USAGE_BY_SESSION
from ..session_meta import _find_meta
from ..usage import cache_hit_ratio, context_state

router = APIRouter()


@router.get("/api/sessions/{session_id}/usage")
async def get_session_usage(session_id: str) -> dict:
    """Per-session cumulative model usage (the TopBar counter). The live
    `usage` WS event only fires on turns; this lets the UI show a session's
    accumulated stats immediately after a session switch.

    Source order (usage-stats-design.md §5): the persistent usage log first —
    it survives runtime restarts, so a session's total stays truthful across
    restarts — falling back to the in-memory accumulator when nothing was
    logged yet. Response shape is unchanged."""
    # 「总 token」按全树口径（2026-10-12 用户实测：ae01d586 下 127 个
    # 子会话占全树 input 的 77%，父自身数字严重误导）。子孙集合走会话
    # 索引的 parent 链；单趟集合聚合（session_totals_many），不为每个
    # 子会话各扫一遍日期段。
    found = _find_meta(session_id)
    if not found and session_id not in _SESSIONS:
        # Unknown session → 404, EXCEPT when the persistent usage log still has
        # rows for it: deleted sessions keep their billing-style usage data
        # (design §3.3), and that read is the one that must keep working.
        tree = usage_store.session_totals(session_id)
        if not tree:
            raise HTTPException(
                status_code=404, detail={"ok": False, "error": "unknown session"}
            )
        tree["descendant_sessions"] = 0
        return {"ok": True, "usage": tree, "context": _context_of_session(session_id)}
    desc: list[dict] = []
    if found:
        _meta, _slug = found
        try:
            from ..session_meta import _session_meta_descendants

            desc = _session_meta_descendants(_slug, session_id)
        except Exception:  # noqa: BLE001 — 索引不可用时退回仅本会话
            desc = []
    ids = {session_id, *(d.get("id") for d in desc if d.get("id"))}
    tree = (
        usage_store.session_totals_many(ids)
        if len(ids) > 1
        else usage_store.session_totals(session_id)
    )
    ctx = _context_of_session(session_id)
    if tree:
        tree["descendant_sessions"] = len(desc)
        return {"ok": True, "usage": tree, "context": ctx}
    acc = _USAGE_BY_SESSION.get(session_id)
    if not acc:
        return {"ok": True, "usage": None, "context": ctx}
    return {"ok": True, "usage": {**acc, "cache_hit_ratio": cache_hit_ratio(acc)},
            "context": ctx}


# 「当前上下文」只统计跑过会话 graph 的真实对话调用。绕过 graph 的直连调用
# （压缩摘要/标题/探活/记忆/外部 agent/workflow 内部 agent/subagent 调度/分类
# 器/mods 统计）的 input 是被压全量、外部 prompt 或摘要 transcript——都不是
# 本会话上下文。ContextRing 锯齿根因（2026-11-07）：手动 /compact 的命令 turn
# 不跑 graph、没有对话 record，压缩 record（input=被压全量，如 360k/600k=60%）
# 成为最后一条 → 切会话时 GET /usage seed 出被压前的高占用，与 WS usage 帧的
# 压缩后真值（13%）交替闪烁。channels 投递（schedule/wecom/dingtalk/task/…）
# 走 run_user_turn → 真实上下文，不在名单内。
_NON_CONTEXT_SOURCES = frozenset({
    "compaction", "external", "memory", "title", "probe", "classifier",
    "mods", "subagent_gate", "subagent_plan", "workflow",
})


def _context_of_session(session_id: str) -> dict | None:
    """TopBar 圆环的初始值：最近一次**对话调用**的全量 prompt 占用（与 WS
    usage 事件的 context 字段同口径，见 usage.context_state）。无对话调用
    记录 → None（前端不显示圆环）——只有压缩/标题类 sideband 记录时宁可
    不显示，也不显示被压全量之类的假占用。"""
    try:
        s = _SESSIONS.get(session_id)
        provider = (s or {}).get("model_provider") or ""
        model = (s or {}).get("model_name") or ""
        if not s:
            found = _find_meta(session_id)
            if found:
                provider = found[0].get("provider") or ""
                model = found[0].get("model") or ""
        rows = usage_store.query_requests(
            session_id=session_id, page=1, page_size=20).get("rows") or []
        # 跳过非对话 source（见 _NON_CONTEXT_SOURCES）：page_size=20 的窗口
        # 足够跨过「手动 /compact 后 + 一串 sideband 调用」的最后一段；连续
        # 20+ 条全是 sideband 的会话退化为 None（环不显示），仍优于假值。
        row = next(
            (r for r in rows if r.get("source") not in _NON_CONTEXT_SOURCES),
            None,
        )
        if row is None:
            return None
        # usage log 的 input_tokens 已是全量 prompt（含缓存读/写）
        return context_state(int(row.get("input_tokens") or 0), provider, model)
    except Exception:  # noqa: BLE001 — 显示辅助
        return None


def _usage_session_display(sid: str) -> dict:
    """Join display meta for a usage row. Deleted sessions keep their usage
    (billing-style data) under a placeholder title (design §3.3)."""
    s = _SESSIONS.get(sid)
    if s:
        return {
            "title": s.get("title") or "",
            "icon": s.get("icon") or "message-square",
            "agent_id": s.get("agent_id"),
            "provider": s.get("model_provider") or "",
            "model": s.get("model_name") or "",
            "deleted": False,
        }
    found = _find_meta(sid)
    if found:
        m, _slug = found
        return {
            "title": m.get("title") or "",
            "icon": m.get("icon") or "message-square",
            "agent_id": m.get("agent_id"),
            "provider": m.get("provider") or "",
            "model": m.get("model") or "",
            "deleted": False,
        }
    return {
        "title": t(f"(deleted) {sid[:6]}", f"(已删除) {sid[:6]}"),
        "icon": "message-square",
        "agent_id": None,
        "provider": "",
        "model": "",
        "deleted": True,
    }


def _provider_display_names() -> dict[str, str]:
    """provider id → 人读名（settings 里配置的名称,含 legacy 槽位的缺省名）。

    统计/日志按 id 分桶（键稳定、可过滤）,但展示层直接画 ``prov_2a60a2a4``
    这种 id 没法认（2026-11-03 用户反馈）——API 层统一附 ``provider_name``,
    前端拿名字展示、拿 id 兜底。读取失败/未知 id 一律回退原值。"""
    try:
        from .. import providers as prov_mod

        return {
            pid: str(cfg.get("name") or "").strip() or pid
            for pid, cfg in prov_mod.load_providers().items()
        }
    except Exception:  # noqa: BLE001 — 展示性装饰,绝不让统计接口报错
        return {}


def _decorate_provider_name(rows: list[dict], names: dict[str, str]) -> None:
    for r in rows:
        pid = str(r.get("provider") or "")
        if pid:
            r["provider_name"] = names.get(pid, pid)


@router.get("/api/usage/overview")
async def usage_overview(days: int = 30) -> dict:
    """KPI + daily series + provider/model breakdown for the trailing window
    (design §5). Window is clamped to [1, retention]."""
    days = max(1, min(int(days), usage_store.RETENTION_DAYS))
    from .. import pricing

    agg = usage_store.aggregate_overview(days)
    names = _provider_display_names()
    _decorate_provider_name(agg.get("providers") or [], names)
    _decorate_provider_name(agg.get("models") or [], names)
    for day in agg.get("daily") or []:
        _decorate_provider_name(day.get("models") or [], names)
    return {
        "ok": True,
        **agg,
        "pricing_configured": pricing.configured(),
        "currency": "CNY",
    }


@router.get("/api/usage/grid")
async def usage_grid(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict:
    """点格图数据：连续日历 × 每天 12 个 2 小时桶（usage-cadence-design.md §7）。
    默认最近 30 天，窗口上限对齐保留期。无 jsonl 文件的日子照样出现在 days 里，
    整列全 0——前端对「无文件」和「零请求」渲染同一个空格子。"""
    return {"ok": True, **usage_store.aggregate_grid(from_, to)}


@router.get("/api/usage/sessions")
async def usage_sessions(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    sort: str = "total",
    limit: int = 200,
) -> dict:
    """Per-session aggregates over [from, to] (defaults: full retention window
    ending today), joined with display meta."""
    rows = usage_store.aggregate_sessions(from_, to, sort=sort, limit=limit)
    for r in rows:
        sid = r.get("session_id")
        r.update(_usage_session_display(sid) if sid else {
            "title": t("(background/system)", "(后台/系统)"), "icon": "cpu", "agent_id": None,
            "provider": "", "model": "", "deleted": False,
        })
    return {"ok": True, "sessions": rows}


@router.get("/api/usage/sessions/{session_id}")
async def usage_session_detail(
    session_id: str,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict:
    """One session's aggregate within [from, to] (defaults: full retention)."""
    totals = usage_store.session_totals(session_id, from_, to)
    if totals is None:
        return {"ok": True, "usage": None}
    return {"ok": True, "usage": totals, **_usage_session_display(session_id)}


@router.get("/api/usage/requests")
async def usage_requests(
    date: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Request log for one day with filters + pagination (design §3.4)."""
    res = usage_store.query_requests(
        date_str=date, provider=provider, model=model, source=source,
        session_id=session_id, page=page, page_size=page_size,
    )
    _decorate_provider_name(res.get("rows") or [], _provider_display_names())
    return {"ok": True, **res}
