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
    """Warn only when the *current* context nears the model window.

    Session totals are cumulative across every call in the session: fifty
    turns sum to dozens of windows while each individual prompt stays small
    (2026-10-24: a session reported 46401k tokens ≈ 9063% of a 512k window —
    it compared lifetime usage against one window). Context occupancy is the
    whole prompt of the newest LLM call; ``input_tokens`` is normalized to
    include cache reads/writes (see usage.py), so it is exactly that number.
    """
    sid = str(meta.get("id") or "")
    latest = usage_store.latest_record(sid) or {}
    used = int(latest.get("input_tokens") or 0)
    if used <= 0:
        return None
    window = 0
    try:
        cfg = prov_mod.get_config(str(meta.get("provider") or ""))
        window = prov_mod.context_window_for(cfg, str(meta.get("model") or ""))
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
        "detail": (
            f"当前上下文约 {used / 1000:.0f}k tokens，"
            f"占模型窗口（{window / 1000:.0f}k）的 {ratio * 100:.0f}%。"
        ),
        "action": {"type": "compact"},
    }


def suggestions_for(session_id: str) -> dict:
    found = _find_meta(session_id)
    if not found:
        return {"ok": False, "error": "unknown session", "suggestions": []}
    meta, _slug = found
    if _is_automation(meta):
        # Explicit contract: automation sessions never get suggestions.
        return {"ok": True, "excluded": "automation", "suggestions": []}
    out = [
        s
        for s in (
            _failure_suggestion(meta),
            _context_suggestion(meta),
        )
        if s
    ]
    return {"ok": True, "suggestions": out[:_MAX_SUGGESTIONS]}
