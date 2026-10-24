"""Execution-time suggestions (V2 P2-1).

Suggestions must be actionable and must never nag the automation paths. Two
hard rules from the backlog review:

* sessions that belong to a scheduled task or a workflow are excluded
  (``schedule_run_id`` / ``workflow_id``) — their "repetition" is the product
  working as designed;
* every suggestion carries a concrete action the UI can execute (switch model,
  compact, distill to skill), never a generic tip.
"""

from __future__ import annotations

from . import providers as prov_mod
from . import usage_store
from .session_meta import _find_meta

_MAX_SUGGESTIONS = 3


def _is_automation(meta: dict) -> bool:
    return bool(
        meta.get("schedule_run_id")
        or meta.get("workflow_id")
        or str(meta.get("type") or "") in ("scheduled",)
    )


def _failure_suggestion(meta: dict) -> dict | None:
    err = meta.get("last_error") or {}
    msg = str(err.get("message") or "")
    if not msg:
        return None
    low = msg.lower()
    if "401" in low or "authentication" in low or "invalid api-key" in low:
        return {
            "id": "failure.auth",
            "severity": "high",
            "title": "模型密钥被拒",
            "detail": "上次运行 401：当前 provider 的 API key 无效或被拒。",
            "action": {"type": "open_settings", "target": "model-api"},
        }
    if "context_length" in low or "maximum context" in low or "too many tokens" in low:
        return {
            "id": "failure.context_length",
            "severity": "high",
            "title": "上下文超限",
            "detail": "上次运行因上下文过长失败：建议先压缩或新开会话。",
            "action": {"type": "compact"},
        }
    if "data_inspection_failed" in low or "inappropriate content" in low:
        return {
            "id": "failure.content_policy",
            "severity": "medium",
            "title": "内容审核拦截",
            "detail": "供应商审核拦截了请求：可换一个模型或拆分任务。",
            "action": {"type": "switch_model"},
        }
    return None


def _context_suggestion(meta: dict) -> dict | None:
    sid = str(meta.get("id") or "")
    totals = usage_store.session_totals(sid) or {}
    used = int(totals.get("input_tokens") or 0) + int(totals.get("output_tokens") or 0)
    if used <= 0:
        return None
    window = 0
    try:
        for cfg in prov_mod.load_configs():
            if cfg.get("id") == meta.get("provider"):
                window = int(cfg.get("context_window") or 0)
                break
    except Exception:  # noqa: BLE001 — display-only
        window = 0
    if window <= 0:
        return None
    ratio = used / window
    if ratio < 0.7:
        return None
    return {
        "id": "context.pressure",
        "severity": "medium",
        "title": "上下文接近上限",
        "detail": f"本会话累计 {used / 1000:.0f}k tokens，约占模型窗口的 {ratio * 100:.0f}%。",
        "action": {"type": "compact"},
    }


def _repetition_suggestion(slug: str, sid: str) -> dict | None:
    """Same tool used ≥3 times recently → offer to distil it into a skill."""
    try:
        from .checkpointer import FileCheckpointer

        tup = FileCheckpointer(slug).get_tuple(
            {"configurable": {"thread_id": sid}}
        )
    except Exception:  # noqa: BLE001
        return None
    if not tup or not tup.checkpoint:
        return None
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    names: list[str] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            name = str(tc.get("name") or "")
            if name and not name.startswith("render_"):
                names.append(name)
    tail = names[-12:]
    counts: dict[str, int] = {}
    for n in tail:
        counts[n] = counts.get(n, 0) + 1
    repeat = next(((n, c) for n, c in counts.items() if c >= 3), None)
    if not repeat:
        return None
    name, count = repeat
    return {
        "id": "repetition.tool",
        "severity": "low",
        "title": "重复的手工步骤",
        "detail": f"最近使用 `{name}` {count} 次：可以把这套流程沉淀为 skill。",
        "action": {"type": "distill", "kind": "skill"},
    }


def suggestions_for(session_id: str) -> dict:
    found = _find_meta(session_id)
    if not found:
        return {"ok": False, "error": "unknown session", "suggestions": []}
    meta, slug = found
    if _is_automation(meta):
        # Explicit contract: automation sessions never get suggestions.
        return {"ok": True, "excluded": "automation", "suggestions": []}
    out = [
        s
        for s in (
            _failure_suggestion(meta),
            _context_suggestion(meta),
            _repetition_suggestion(slug, session_id),
        )
        if s
    ]
    return {"ok": True, "suggestions": out[:_MAX_SUGGESTIONS]}
