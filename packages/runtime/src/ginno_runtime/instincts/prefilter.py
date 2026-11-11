"""Instincts L2 预筛（docs/instincts-design.md §5 L2）。

廉价确定性预筛：用户消息命中纠偏信号才进入 observer 小模型判定。
三条规则（v1 实现前两条；第三条「同 turn deny」在 capture 侧天然有观察）：
1. 否定/纠偏词命中；
2. 观察流最近存在 tool_denied（用户在拒绝后改口 = 纠偏场景）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 中英双语纠偏词（§5 原文词表 + 常见变体；宁滥勿缺——observer 判定兜底）。
_CORRECTION_RE = re.compile(
    r"不对|别这?样|不要|别再|撤销|说错了?|我说的不是|不是这个|重来|错了|停|"
    r"\b(no|stop|wrong|undo|revert|not that|don'?t)\b",
    re.IGNORECASE,
)


def correction_reason(user_text: str, recent_obs: list[dict[str, Any]]) -> str | None:
    """返回命中原因（None=不命中）。recent_obs 为观察流尾部若干条。"""
    text = (user_text or "").strip()
    if not text:
        return None
    if _CORRECTION_RE.search(text):
        return "correction-word"
    for ev in reversed(recent_obs[-10:]):
        if ev.get("type") == "tool_denied":
            return "after-deny"
    return None
