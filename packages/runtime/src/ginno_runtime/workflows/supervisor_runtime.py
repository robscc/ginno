"""Auto-mode supervisor adjudicator (design B §8.5, P2.5 / 阶段3).

One structured-JSON LLM call per gate hit: the judge sees the gated node's
output, the run context and the last few events, and answers

    {"decision": "continue"|"skip"|"retry"|"abort",
     "confidence": 0..1, "reason": "...", "context_patch": {...}|null}

:func:`adjudicate` RAISES on ANY failure (unparseable JSON, invalid decision,
out-of-range confidence) — the gate's fallback ladder
(:mod:`ginno_runtime.workflows.nodes.supervisor_gate`) catches and escalates
to a human park. Tests monkeypatch
``ginno_runtime.workflows.supervisor_runtime.adjudicate`` — the gate calls it
through the module attribute, never via ``from ... import adjudicate``.
"""

from __future__ import annotations

import json

from ..lang import t

_DECISIONS = ("continue", "skip", "retry", "abort")

_DEFAULT_POLICY = (
    "你是工作流监督者：审查刚执行完的节点输出，判断运行是否可信地继续。"
    "输出正确/可接受 → continue；输出缺失或失败且重跑可能改善 → retry；"
    "该步骤不必要或其结果应被忽略 → skip；运行应立即终止 → abort。拿不准时选 continue 并给出较低 confidence。"
)
_DEFAULT_POLICY_EN = (
    "You are the workflow supervisor: review the output of the node that just ran "
    "and judge whether the run can credibly continue.\n"
    "Output correct/acceptable → continue; output missing or failed and a rerun "
    "might improve it → retry; the step is unnecessary or its result should be "
    "ignored → skip; the run must stop immediately → abort. When unsure, choose "
    "continue with a low confidence."
)

# Appended to whichever policy variant is selected — machine skeleton (decision
# vocabulary, JSON shape and key names) is identical in both variants.
_CONTRACT_ZH = (
    "\n\n## 决策词汇表\n"
    "continue | skip | retry | abort（只能取其中一个值）\n\n"
    "## 输出契约\n"
    "只输出一个 JSON 对象，不要输出任何其他文字、不要代码围栏：\n"
    '{"decision": "continue|skip|retry|abort", "confidence": <0到1的小数>, '
    '"reason": "<不超过200字的理由>", "context_patch": <要合并进运行上下文的对象或null>}'
)
_CONTRACT_EN = (
    "\n\n## Decision vocabulary\n"
    "continue | skip | retry | abort (exactly one of these values)\n\n"
    "## Output contract\n"
    "Output a single JSON object — no other text, no code fences:\n"
    '{"decision": "continue|skip|retry|abort", "confidence": <float in 0..1>, '
    '"reason": "<reason, no more than 200 chars>", '
    '"context_patch": <object to merge into the run context, or null>}'
)

# Rendered context values / event summaries are compacted to keep the judge
# prompt small (the gated output alone may already be 2000 chars).
_CTX_VALUE_MAX = 300
_CTX_KEY_MAX = 20
_EVENT_LINE_MAX = 120


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_json_object(text: str) -> str | None:
    """First brace-balanced ``{...}`` in ``text`` (string-aware) — tolerates
    code fences and prose around the JSON (models wrap despite instructions)."""
    j = text.find("{")
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[j : k + 1]
    return None


def _judge_model(cfg: dict, cctx: dict):
    """Resolve the judge model: a configured provider model when
    ``cfg["model"]`` names one (same resolution as extract's ``extract_model``),
    else the run's own forked model."""
    name = (cfg or {}).get("model")
    if isinstance(name, str) and name.strip():
        from ..models import build_model_by_name

        return build_model_by_name(name.strip())
    return cctx.get("model")


def _render_events(recent_events: list) -> str:
    lines = []
    for ev in recent_events or []:
        if not isinstance(ev, dict):
            continue
        gist = (
            ev.get("reason")
            or ev.get("decision")
            or ev.get("question")
            or ev.get("error")
            or ev.get("title")
            or ""
        )
        lines.append(
            f"- {ev.get('kind')} node={ev.get('node_id')}"
            + (f" | {_truncate(str(gist), _EVENT_LINE_MAX)}" if gist else "")
        )
    return "\n".join(lines) if lines else t("(none)", "（无）")


def _render_context(context: dict) -> str:
    lines = []
    for i, (k, v) in enumerate(dict(context or {}).items()):
        if i >= _CTX_KEY_MAX:
            lines.append(
                t(
                    f"- …({len(context) - _CTX_KEY_MAX} more keys omitted)",
                    f"- …（其余 {len(context) - _CTX_KEY_MAX} 个键省略）",
                )
            )
            break
        lines.append(f"- {k}: {_truncate(str(v), _CTX_VALUE_MAX)}")
    return "\n".join(lines) if lines else t("(empty)", "（空）")


async def adjudicate(
    *,
    node_id: str,
    node_output,
    context: dict,
    recent_events: list,
    policy: str,
    model,
    run_ctx: dict,
    node_type: str | None = None,
) -> dict:
    """One structured-JSON adjudication of a gated node's output.

    Returns ``{decision, confidence, reason, context_patch, usage}`` where
    ``usage`` is ``{input_tokens, output_tokens}`` (0/absent keys treated as 0
    by the gate's token budget). Raises on ANY failure — the gate's ladder
    catches and escalates to human.

    ``node_type`` is optional extra prompt context (the spec'd signature ends
    at ``run_ctx``; the gate passes it when it knows the gated node's type).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..graph import text_of_content
    from .nodes import agent_helpers as ah
    from .nodes.base import llm_invoke_with_timeout

    policy_text = (policy or "").strip() or t(_DEFAULT_POLICY_EN, _DEFAULT_POLICY)
    system = policy_text + t(_CONTRACT_EN, _CONTRACT_ZH)
    output_text = _truncate(
        str(node_output) if node_output is not None else "", 2000
    ) or t("(empty)", "（空）")
    user = (
        t(f"## Supervised node\nid: {node_id}", f"## 被监督节点\nid: {node_id}")
        + (f"  type: {node_type}" if node_type else "")
        + "\n\n"
        + t(
            f"## Gated node output (truncated when long)\n{output_text}",
            f"## 该节点的输出（过长已截断）\n{output_text}",
        )
        + "\n\n"
        + t("## Current run context\n", "## 当前运行上下文\n")
        + _render_context(context)
        + "\n\n"
        + t("## Recent events\n", "## 最近事件\n")
        + _render_events(recent_events)
        + "\n\n"
        + t("Respond with the adjudication JSON.", "请给出裁决 JSON。")
    )
    msgs = [SystemMessage(content=system), HumanMessage(content=user)]
    resp = await llm_invoke_with_timeout(model.ainvoke(msgs))
    u = ah.record_model_usage(resp, (run_ctx or {}).get("usage_attr")) or {}
    text = text_of_content(resp.content)

    frag = _first_json_object(text)
    if not frag:
        raise ValueError(f"adjudicator returned no JSON object: {_truncate(text, 200)!r}")
    try:
        data = json.loads(frag)
    except json.JSONDecodeError as exc:
        raise ValueError(f"adjudicator JSON unparseable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("adjudicator JSON is not an object")

    decision = data.get("decision")
    if decision not in _DECISIONS:
        raise ValueError(f"adjudicator decision invalid: {decision!r}")
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ValueError(f"adjudicator confidence invalid: {data.get('confidence')!r}") from None
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"adjudicator confidence out of range: {confidence}")
    reason = data.get("reason")
    patch = data.get("context_patch")
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": _truncate(str(reason or ""), 200),
        "context_patch": patch if isinstance(patch, dict) else None,
        "usage": {
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
        },
    }
