"""阶段 3/4：流出口与看门狗（批次 5-1 空壳）。

迁移目标（5-2 从 ``engine._stream_graph_legacy`` 复制式搬入）：
- ``emit`` / ``safe_send`` 闭包与 ``frame_session`` 戳、event_sink 喂帧；
- chunked stream 主循环 + stall watchdog（``CHUNK_TIMEOUT_S``，慢网关/
  限流退避误伤的豁免口径）；
- token.delta / thinking.delta 合帧（``DeltaCoalescer``，33ms 窗口，
  非 delta 事件前 flush 保序）；
- 15s keepalive 协程（前端 45s watchdog 线内重置 lastSeen）；
- ``ws_closed`` 的「最近一次广播有无活 socket」诊断记录（从不锁存）。
"""

from __future__ import annotations

from .context import StreamTurnCtx


async def run(ctx: StreamTurnCtx) -> None:
    raise NotImplementedError(
        "stages.streamio.run — 批次 5-2 迁移目标（jt5-stages-base 仅骨架）"
    )
