"""Instincts Observer 分析器（docs/instincts-design.md §6，P1 子集）。

位点管理（observer-state.json）+ 节流（新观察 ≥ min_observations 且距上次
≥ analyze_gap_min）+ 分派：确定性 tool_denied 走模板（不调 LLM，复用
maybe_analyze）；user_correction 走 observer 小模型起草 trigger/behavior。
- 匹配既有条目（trigger 互含）→ 强化，不新建；
- usage 记账 source="instincts"（照 memory 蒸馏 source="memory" 先例）；
- 失败保留位点可重试（防停摆：成功才前进）。
冲突冻结（§6「矛盾→conflict」）留 P1 后续——v1 用「同 trigger 强化」
避免重复条目，语义冲突检测需 LLM 对比，暂缓。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import usage as usage_mod
from ..lang import t
from ..models import build_model
from .capture import _confidence_for, _id_for_sig, _tool_denied_signature, _ts, observe
from .store import get_instinct, list_instincts, observations_path, save_instinct
from .types import Instinct, load_instincts_config

_STATE_VERSION = 1


def _state_path():
    from .store import instincts_root

    return instincts_root() / "observer-state.json"


def _load_state() -> dict[str, Any]:
    p = _state_path()
    try:
        d = json.loads(p.read_text() or "{}")
    except Exception:
        d = {}
    return {
        "version": _STATE_VERSION,
        "offset": int(d.get("offset", 0)),
        "last_analyze_ts": float(d.get("last_analyze_ts", 0.0)),
        "last_error": str(d.get("last_error", "")),
    }


def _save_state(st: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


_OBSERVER_PROMPT_EN = (
    "You distill ONE user-correction into a behavioral rule for an AI agent. "
    "Input: what the agent did (before), what the user then said (after). "
    'Reply STRICT JSON: {"is_rule": bool, "trigger": str, "behavior": str, '
    '"domain": "tool-usage|code-style|workflow|security|user-preference", '
    '"scope": "project|global"}. If the correction is too vague to generalize, '
    "is_rule=false."
)
_OBSERVER_PROMPT = (
    "你把一次用户纠偏蒸馏为 AI agent 的行为规则。输入：agent 之前做了什么"
    "（before）、用户随后说了什么（after）。只回复 JSON："
    '{"is_rule": bool, "trigger": str, "behavior": str, '
    '"domain": "tool-usage|code-style|workflow|security|user-preference", '
    '"scope": "project|global"}。若纠偏太具体/不可泛化，is_rule=false。'
)


async def _judge_correction(
    before: str, after: str, cfg, session_id: str
) -> dict[str, Any] | None:
    """observer 小模型判定；返回规则草稿或 None（不可泛化/失败）。"""
    prov = (cfg.observer_model or "").strip()
    if not prov:
        return None
    model = build_model(prov, (cfg.observer_model_id or "").strip() or None)
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = await model.ainvoke(
        [
            SystemMessage(content=t(_OBSERVER_PROMPT_EN, _OBSERVER_PROMPT)),
            HumanMessage(content=f"## Before\n{before[:600]}\n\n## After\n{after[:600]}"),
        ]
    )
    usage_mod.record_message(resp, source="instincts", provider=prov,
                             session_id=session_id)
    raw = getattr(resp, "content", "") or ""
    # 提取第一个 JSON 对象（模型可能裹 markdown 代码块）。
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        d = json.loads(raw[s : e + 1])
    except Exception:
        return None
    if not d.get("is_rule"):
        return None
    trigger = str(d.get("trigger") or "").strip()
    behavior = str(d.get("behavior") or "").strip()
    if not trigger or not behavior:
        return None
    return {
        "trigger": trigger[:200],
        "behavior": behavior[:300],
        "domain": d.get("domain") if d.get("domain") in (
            "tool-usage", "code-style", "workflow", "security", "user-preference"
        ) else "tool-usage",
        "scope": d.get("scope") if d.get("scope") in ("project", "global") else "project",
    }


def _matches_existing(trigger: str) -> Instinct | None:
    """trigger 互含（一方包含另一方）即视为同条目——强化而非新建。"""
    for inst in list_instincts():
        if inst.status not in ("active", "pending", "evolved"):
            continue
        a, b = inst.trigger.strip(), trigger.strip()
        if a and b and (a in b or b in a):
            return inst
    return None


async def run_observer(session_id: str = "") -> dict[str, Any]:
    """位点驱动的一次分析（detached 线程调用；失败保留位点）。"""
    cfg = load_instincts_config()
    if not cfg.enabled:
        return {"ok": False, "skipped": "disabled"}
    p = observations_path()
    if not p.exists():
        return {"ok": False, "skipped": "no-observations"}
    lines = p.read_text(encoding="utf-8").splitlines()
    st = _load_state()
    offset = min(st["offset"], len(lines))
    fresh = lines[offset:]
    if len(fresh) < cfg.min_observations:
        return {"ok": False, "skipped": "few-observations", "fresh": len(fresh)}
    if time.time() - st["last_analyze_ts"] < cfg.analyze_gap_min * 60:
        return {"ok": False, "skipped": "gap"}

    events = []
    for ln in fresh:
        try:
            events.append(json.loads(ln))
        except Exception:
            continue

    created = reinforced = 0
    last_error = ""
    for ev in events:
        try:
            if ev.get("type") == "tool_denied":
                sig = _tool_denied_signature(ev)
                count = sum(
                    1 for x in events if x.get("type") == "tool_denied"
                    and _tool_denied_signature(x) == sig
                )
                if count < cfg.template_threshold:
                    continue
                slug = ev.get("project") or "default"
                iid = _id_for_sig(sig)
                cur = get_instinct("project", slug, iid)
                if cur is not None and cur.status in ("active", "pending", "evolved"):
                    cur.evidence["observed"] = count
                    cur.evidence["last_seen"] = _ts()
                    cur.confidence = _confidence_for(count, cfg.template_threshold)
                    save_instinct(cur)
                    reinforced += 1
                elif cur is None:
                    save_instinct(Instinct(
                        id=iid,
                        trigger=f"即将执行 {ev.get('tool') or '?'}（参数形如「{str(ev.get('args_repr') or '')[:120]}」类）时",
                        behavior="执行前先向用户说明将做什么并获得确认；该操作模式此前被用户拒绝过多次",
                        confidence=_confidence_for(count, cfg.template_threshold),
                        domain="tool-usage", scope="project", project_slug=slug,
                        source="permission-deny", status="pending",
                        evidence={"observed": count, "applied": 0, "uncorrected": 0,
                                  "corrected": 0, "last_seen": _ts()},
                    ))
                    created += 1
            elif ev.get("type") == "user_correction":
                draft = await _judge_correction(
                    str(ev.get("before") or ""), str(ev.get("after") or ""),
                    cfg, session_id,
                )
                if draft is None:
                    continue
                existing = _matches_existing(draft["trigger"])
                if existing is not None:
                    existing.evidence["observed"] = int(existing.evidence.get("observed", 0)) + 1
                    existing.evidence["last_seen"] = _ts()
                    save_instinct(existing)
                    reinforced += 1
                else:
                    save_instinct(Instinct(
                        id=f"corr-{int(time.time() * 1000) % 10**10}",
                        trigger=draft["trigger"], behavior=draft["behavior"],
                        confidence=0.5, domain=draft["domain"], scope=draft["scope"],
                        project_slug=(ev.get("project") or "default") if draft["scope"] == "project" else "",
                        source="user-correction", status="pending",
                        evidence={"observed": 1, "applied": 0, "uncorrected": 0,
                                  "corrected": 0, "last_seen": _ts()},
                    ))
                    created += 1
        except Exception as exc:  # noqa: BLE001 — 单条失败不挡整批
            last_error = str(exc)[:200]

    # 成功才前进位点（失败可重试）；节流窗口以成功批次计。
    st["offset"] = len(lines)
    st["last_analyze_ts"] = time.time()
    st["last_error"] = last_error
    _save_state(st)
    return {"ok": True, "created": created, "reinforced": reinforced,
            "processed": len(events), "error": last_error}


def capture_correction_signal(user_text: str, session_id: str = "") -> bool:
    """turn 收尾预筛：命中纠偏 → observe(user_correction)。返回是否捕获。"""
    try:
        cfg = load_instincts_config()
        if not (cfg.enabled and cfg.capture):
            return False
        from .prefilter import correction_reason

        p = observations_path()
        recent: list[dict[str, Any]] = []
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines()[-10:]:
                try:
                    recent.append(json.loads(ln))
                except Exception:
                    continue
        reason = correction_reason(user_text or "", recent)
        if reason is None:
            return False
        before = ""
        for ev in reversed(recent):
            if ev.get("type") == "tool_denied":
                before = f"tool {ev.get('tool')} args={str(ev.get('args_repr') or '')[:120]} 被拒"
                break
        observe({
            "type": "user_correction",
            "reason": reason,
            "before": before,
            "after": (user_text or "")[:300],
            "session": session_id,
            "project": "",
            "turn": "",
        })
        return True
    except Exception:
        return False


def maybe_run_observer(session_id: str = "") -> None:
    """同步入口（kick 线程调用）：异步 run_observer 的兜底包装。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    coro = run_observer(session_id)
    if loop is not None:
        loop.create_task(coro)
    else:
        asyncio.run(coro)
