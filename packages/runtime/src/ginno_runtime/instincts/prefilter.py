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


def is_system_injected(text: str) -> bool:
    """系统注入文本判定（2026-11-15 P0 降噪）：子代理/worker brief 与结果
    回注、任务状态回注、长角色设定 prompt 都不是用户纠偏——它们含
    「不要/别」等词会误命中预筛，曾占观察流 24/25。捕获侧与分析侧同口径
    过滤（存量噪声也不喂 LLM）。
    """
    t = (text or "").strip()
    # Ginno 内部协议注入块（<ginno_subagent_*/worker_*/goal/steer/plan/
    # citations 等，见 graph_messages/tasks/inject 的包装标记）
    if "<ginno_" in t:
        return True
    # 后台任务状态回注（[task finished]/[task stopped by user]/[task watch]…）
    if t.startswith("[task "):
        return True
    # 「你是……」开头的长文本 = 角色设定/任务简报，不是纠偏指令
    if t.startswith("你是") and len(t) > 200:
        return True
    return False


def correction_reason(user_text: str, recent_obs: list[dict[str, Any]]) -> str | None:
    """返回命中原因（None=不命中）。recent_obs 为观察流尾部若干条。"""
    text = (user_text or "").strip()
    if not text or is_system_injected(text):
        return None
    if _CORRECTION_RE.search(text):
        return "correction-word"
    for ev in reversed(recent_obs[-10:]):
        if ev.get("type") == "tool_denied":
            return "after-deny"
    return None
