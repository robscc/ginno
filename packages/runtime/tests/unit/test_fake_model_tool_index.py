"""fake_model 多 tool_call 的 index 分配回归（批次 0 登记，2026-11-21 修复）。

缺陷：ScriptedChatModel._stream 里两个 chunk 的 tool_call_chunks 曾硬编码
``"index": 0``——单 tool_call 恰好正确，但**同一消息多个 tool_call** 时全部
落 index 0，LangGraph 按 index 合并流式块会把并行工具调用误合流为一个
（args 拼接/name 取首个），测试桩行为失真。修复为 ``index: i``（本回归钉住）。
"""

from __future__ import annotations

import json

from ginno_runtime.testing.fake_model import (
    ScriptedChatModel,
    script,
    script_tool_call,
)


def test_multi_tool_call_chunks_get_distinct_indices():
    model = ScriptedChatModel(
        scripts=[
            script(
                text="",
                tool_calls=[
                    script_tool_call("bash", {"command": "echo a"}, call_id="c_a"),
                    script_tool_call("read_file", {"path": "/x"}, call_id="c_b"),
                ],
            )
        ]
    )
    chunks = list(model.stream([]))
    assert chunks, "fake model 流式至少产出一块"

    # 收集所有 tool_call_chunks 的 index（name-first 与 args 两类）
    pairs: list[tuple[int, str | None, str]] = []
    for msg in chunks:
        tcc = getattr(msg, "tool_call_chunks", None) or []
        for c in tcc:
            args = c.get("args") or ""
            pairs.append((c["index"], c.get("name"), args))
    # 每个工具两个 chunk（name-first + args），各自带独立 index
    assert len(pairs) == 4, pairs
    idxs = {i for i, _, _ in pairs}
    assert idxs == {0, 1}, f"多 tool_call 必须分到不同 index，实际 {idxs}"
    # name-first 块：index 与工具名一一对应
    names = {i: n for i, n, a in pairs if n}
    assert names == {0: "bash", 1: "read_file"}, names
    # args 块：落到正确的 index 且 JSON 可解析、内容正确
    arg_map = {i: a for i, n, a in pairs if not n}
    assert json.loads(arg_map[0]) == {"command": "echo a"}
    assert json.loads(arg_map[1]) == {"path": "/x"}


def test_single_tool_call_still_index_zero():
    model = ScriptedChatModel(
        scripts=[script(text="", tool_calls=[script_tool_call("bash", {"command": "x"})])]
    )
    chunks = list(model.stream([]))
    idxs = set()
    for msg in chunks:
        for c in getattr(msg, "tool_call_chunks", None) or []:
            idxs.add(c["index"])
    assert idxs == {0}, idxs
