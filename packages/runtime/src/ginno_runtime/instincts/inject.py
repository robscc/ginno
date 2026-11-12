"""Instincts 注入管道（docs/instincts-design.md §8，P0）。

挂点 graph_prompts.build_turn_context（与 wiki 注入同层、同范式）；
volatile turn-context 层不进稳定 system 层（保 prefix cache）。
排序 confidence × relevance（词汇重叠，纯本地）；零命中不注入。
"""

from __future__ import annotations

from ..knowledge.injection import _INJECTION_PATTERNS
from .store import list_instincts
from .types import load_instincts_config


def _relevance(trigger: str, behavior: str, query: str) -> float:
    """trigger/behavior 与当前 query 的词汇重叠（0-1，本地无模型）。"""
    if not query:
        return 0.0
    q = set(query.lower().split())
    if not q:
        return 0.0
    words = set((trigger + " " + behavior).lower().split())
    overlap = len(q & words)
    return min(1.0, overlap / max(4, len(q) // 2))


def select_for_injection(query: str, project_slug: str = "") -> list:
    """按当前配置选出会被注入的条目（build_instinct_context 同口径）。

    抽出供两处复用：注入本体 + turn 收尾的 applied/uncorrected 归因
    （P1 收尾：本 turn 注入过的条目，无纠偏 → applied+1/uncorrected+1，
    有纠偏 → corrected+1——evidence 闭环，confidence 引擎的数据源）。
    """
    cfg = load_instincts_config()
    if not (cfg.enabled and cfg.inject_enabled):
        return []
    cands = [
        i
        for i in list_instincts(status="active")
        if i.confidence >= cfg.inject_min_confidence
        and (i.scope == "global" or not project_slug or i.project_slug == project_slug)
    ]
    if not cands:
        return []
    scored = sorted(
        ((i.confidence * (0.5 + 0.5 * _relevance(i.trigger, i.behavior, query)), i) for i in cands),
        key=lambda t: -t[0],
    )[: cfg.inject_max_n]
    return [i for _s, i in scored]


def build_instinct_context(query: str, project_slug: str = "") -> str | None:
    """<instincts> 注入段；无合格条目返回 None（parts.append 跳过）。"""
    try:
        cfg = load_instincts_config()
        scored = [(0.0, i) for i in select_for_injection(query, project_slug)]
        if not scored:
            return None

        lines: list[str] = []
        budget = cfg.inject_token_budget
        for _score, i in scored:
            text = f"- [{i.confidence:.1f}] {i.trigger}：{i.behavior}"
            if i.confidence < 0.6:
                text += "（低置信，用户当前指令优先）"
            # 注入防御：机器生成正文命中 wrapper 伪造模式 → 拒注该条。
            body = i.trigger + i.behavior
            if any(p.search(body) for p in _INJECTION_PATTERNS):
                continue
            est = len(text) // 3 + 8  # 粗 token 估算（中文 ~3 char/token）
            if est > budget:
                break
            budget -= est
            lines.append(text)
        if not lines:
            return None
        head = "历史会话沉淀的行为规则（按置信度排序；与用户当前指令冲突时以用户为准）："
        return "\n".join([head] + lines)
    except Exception:
        return None
