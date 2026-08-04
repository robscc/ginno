"""Middle truncation for tool outputs before they enter history (plan E2).

A single ``read_file``/``bash`` on a large target can otherwise put tens of
thousands of tokens into every subsequent request (history is re-sent in full
each turn). Following Codex's output engineering, we keep the HEAD (command
echo / early errors) and the TAIL (latest, most relevant output) and mark the
cut explicitly so the model knows it is looking at a partial result and can
re-read with a narrower query.

Applied to ToolMessage content in the graph's tools node — the persisted
history and the model view stay identical (unlike image stripping, which is a
send-only optimization).
"""

from __future__ import annotations

TRUNCATION_MARKER = "[输出过长已截断"


def truncate_middle(
    text: str,
    max_chars: int,
    head_ratio: float = 0.6,
) -> str:
    """Keep head+tail of ``text`` when it exceeds ``max_chars``.

    Returns ``text`` unchanged when it fits. The marker line records the
    original length and how much was kept, so the model can reason about the
    gap. ``head_ratio`` splits the kept budget between head and tail.
    """
    if max_chars <= 0:
        return text
    n = len(text)
    if n <= max_chars:
        return text
    head_budget = int(max_chars * min(max(head_ratio, 0.0), 1.0))
    tail_budget = max_chars - head_budget
    head = text[:head_budget]
    tail = text[n - tail_budget:] if tail_budget > 0 else ""
    dropped = n - head_budget - tail_budget
    marker = (
        f"\n{TRUNCATION_MARKER}：原文 {n} 字符，保留头部 {head_budget} + 尾部 {tail_budget}，"
        f"省略中间 {dropped} 字符。如需被省略的内容，请用更精确的查询/范围重新读取。]\n"
    )
    return head + marker + tail


def truncate_tool_content(content, max_chars: int):
    """Truncate a ToolMessage content value (str passes through the policy;
    list/other shapes are returned untouched — our tools emit strings)."""
    if isinstance(content, str):
        return truncate_middle(content, max_chars)
    return content
