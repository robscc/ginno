"""tool.args_delta（2026-11-21 流式参数预览）单测。

背景：大 JSON tool args 生成期可达数十秒，此前只有 tool.start 的 "…"
气泡干等（用户实测 94s 零反馈）。修复：handle_messages_tool_chunks 在
args 累积路径按 300ms 节流下发累计尾部快照（替换式幂等），完整可解析
后 tool.args 终态接管、_emitted_tool_args 门控停止 delta。
"""

from __future__ import annotations

import asyncio
import time
import types

from ginno_runtime.api.stream.stages.ctx import StreamTurnCtx
from ginno_runtime.api.stream.stages.toolloop import handle_messages_tool_chunks


class _Recorder:
    def __init__(self, ctx: StreamTurnCtx) -> None:
        self.ctx = ctx
        self.events: list[dict] = []

    async def __call__(self, ev: dict) -> None:
        self.events.append(ev)


def _make_ctx() -> tuple[StreamTurnCtx, _Recorder]:
    cfg = {
        "configurable": {
            "session_id": "sid-args-delta",
            "thread_id": "sid-args-delta",
            "turn_id": "turn-1",
        }
    }
    ctx = StreamTurnCtx(None, cfg, None, None)
    ctx.session_id = "sid-args-delta"
    ctx.turn_id = "turn-1"
    ctx.ui_turn_id = "turn-1"
    ctx.started_tool_ids = set()
    ctx._tool_name_by_id = {}
    ctx.special_ids = {}
    ctx._partial_tool_args = {}
    ctx._last_args_tail_ts = {}
    ctx._emitted_tool_args = set()
    rec = _Recorder(ctx)
    ctx.safe_send = rec  # type: ignore[method-assign]
    ctx.emit = lambda kind, payload: {"event": kind, "data": payload}  # type: ignore[method-assign]

    class _Coal:
        async def flush(self) -> None:
            return None

    ctx._coal = _Coal()  # type: ignore[assignment]
    return ctx, rec


def _chunk(tcs: list[dict]) -> object:
    return types.SimpleNamespace(tool_call_chunks=tcs, content="")


def _deltas(rec: _Recorder) -> list[dict]:
    return [e["data"] for e in rec.events if e["event"] == "tool.args_delta"]


def _args(rec: _Recorder) -> list[dict]:
    return [e["data"] for e in rec.events if e["event"] == "tool.args"]


def test_first_fragment_fires_start_and_delta() -> None:
    ctx, rec = _make_ctx()
    asyncio.run(
        handle_messages_tool_chunks(
            ctx,
            _chunk([{"id": "t1", "name": "bash", "index": 0, "args": '{"comm'}]),
        )
    )
    kinds = [e["event"] for e in rec.events]
    assert "tool.start" in kinds
    ds = _deltas(rec)
    assert len(ds) == 1  # 首帧立即下发（节流时钟从 0 起）
    assert ds[0]["tail"].endswith('{"comm')
    assert ds[0]["total"] == len('{"comm')


def test_throttle_merges_bursts() -> None:
    ctx, rec = _make_ctx()
    asyncio.run(
        handle_messages_tool_chunks(
            ctx, _chunk([{"id": "t1", "name": "bash", "index": 0, "args": '{"a"'}])
        )
    )
    # 300ms 窗口内的第二段：不重复下发
    asyncio.run(
        handle_messages_tool_chunks(
            ctx, _chunk([{"index": 0, "args": ': 1, "b"'}])
        )
    )
    assert len(_deltas(rec)) == 1
    # 过窗后第三段：快照为累计尾部
    time.sleep(0.35)
    asyncio.run(
        handle_messages_tool_chunks(ctx, _chunk([{"index": 0, "args": ': 2}'}]))
    )
    ds = _deltas(rec)
    assert len(ds) == 2
    assert ds[1]["total"] == len('{"a": 1, "b": 2}')


def test_terminal_args_stops_delta() -> None:
    ctx, rec = _make_ctx()
    full = '{"command": "ls -la /tmp"}'
    for frag in ('{"command": "ls', ' -la /tmp"}'):
        asyncio.run(
            handle_messages_tool_chunks(
                ctx, _chunk([{"id": "t1", "name": "bash", "index": 0, "args": frag}])
            )
        )
        time.sleep(0.35)
    # 完整可解析 → tool.args 终态；此后再有片段不再发任何 delta
    assert len(_args(rec)) == 1
    n = len(_deltas(rec))
    asyncio.run(
        handle_messages_tool_chunks(ctx, _chunk([{"index": 0, "args": " Garbage"}]))
    )
    assert len(_deltas(rec)) == n
