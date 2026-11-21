"""编辑类工具的 unified diff 生成（2026-11-21 编辑 diff 显示 P0/P1）。

为 edit_file / write_file 的 tool.end 载荷生成**纯展示用** diff——不进
工具返回值（content 会写入 checkpoint 并回灌模型上下文，大 diff 全文
进去浪费 token 且干扰模型），走事件载荷的独立 ``diff`` 字段，由前端
DiffView 消费渲染。

数据源（toolloop 调用方负责取）：
- edit_file：args 里的 old/new（精确、零竞态）；
- write_file：args.content 为新内容，旧内容取 files/baselines.py 的
  首写前快照（first-write-wins；无 baseline = 新文件 → 全绿）。

限长契约：MAX_LINES=200 行 / MAX_BYTES=8192 字节，超出截断并标注
（完整内容见 CodePanel / git diff）；added/removed 统计**始终是全量**
（不受截断影响——徽标可信）。
"""

from __future__ import annotations

import difflib
import re

MAX_LINES = 200
MAX_BYTES = 8192

# bash 补丁识别（P2 #3，2026-11-21 用户拍板「做」）：agent 用 bash 跑
# `git apply`/`apply_patch` heredoc 时，补丁体本身就是 unified diff——
# 包装成展示载荷（统计 + 限长），path 从首个 +++ b/<path> 提取。
_PATCH_HEREDOC = re.compile(
    r"<<-?\s*'(\w+)'\s*\n(--- a/.*?\n.*?)\n\1\b", re.S
)


def wrap_patch_diff(patch_text: str) -> dict | None:
    """Wrap an already-unified patch (from a bash heredoc) as the display
    payload. Returns None when the text carries no +/- hunks (not a patch)."""
    full = patch_text.splitlines(keepends=True)
    if not any(l.startswith("+") and not l.startswith("+++") for l in full):
        return None
    path = ""
    for l in full:
        if l.startswith("+++ b/"):
            path = l[6:].strip()
            break
    added = sum(1 for l in full if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in full if l.startswith("-") and not l.startswith("---"))
    text = "".join(full)
    truncated = len(full) > MAX_LINES or len(text.encode("utf-8")) > MAX_BYTES
    if truncated:
        kept = full[:MAX_LINES]
        acc = 0
        cut = len(kept)
        for i, l in enumerate(kept):
            acc += len(l.encode("utf-8"))
            if acc > MAX_BYTES:
                cut = i
                break
        text = "".join(kept[:cut])
    return {
        "op": "patch",
        "path": path or "(bash patch)",
        "text": text,
        "added": added,
        "removed": removed,
        "truncated": truncated,
    }


def make_edit_diff(
    op: str, path: str, old: str | None, new: str | None
) -> dict | None:
    """Build the display-only diff payload. Returns ``None`` when there is
    nothing to show (identical content / unusable inputs) — the caller then
    omits the field entirely (old-sidecar clients degrade gracefully).

    ``op``: "edit" | "add" | "overwrite"（写入语义，仅作展示标注）。
    ``old=None`` 表示新文件（全绿）。
    """
    old_l = (old or "").splitlines(keepends=True)
    new_l = (new or "").splitlines(keepends=True)
    if old_l == new_l:
        return None
    full = list(
        difflib.unified_diff(
            old_l,
            new_l,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    added = sum(1 for l in full if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in full if l.startswith("-") and not l.startswith("---"))
    text = "".join(full)
    truncated = len(full) > MAX_LINES or len(text.encode("utf-8")) > MAX_BYTES
    if truncated:
        kept = full[:MAX_LINES]
        acc = 0
        cut = len(kept)
        for i, l in enumerate(kept):
            acc += len(l.encode("utf-8"))
            if acc > MAX_BYTES:
                cut = i
                break
        kept = kept[:cut]
        # 保留完整的最后一个 hunk 头之前的内容即可；截断标注行由前端
        # 渲染（⌄ 已截断：… 完整内容见 CodePanel / git diff）。
        text = "".join(kept)
    return {
        "op": op,
        "path": path,
        "text": text,
        "added": added,
        "removed": removed,
        "truncated": truncated,
    }
