"""阶段 2/4：agent 超步循环（批次 5-1 空壳）。

迁移目标（5-2 从 ``engine._stream_graph_legacy`` 复制式搬入）：
- LangGraph 超步驱动：tool_call 执行与 tool.start/args/input/end 事件；
- steer 吸收（ack 时序：注入点即时 ack，非 commit 时）、
  ``steer_user_arrived`` 与递归预算的裁决（用户 steer 加预算、系统
  recursion-warn 绝不加）；
- steer-continue / 瞬态错误 auto-retry 的**段重入**（派生 exec turn_id、
  原始 id 进 ABANDONED_TURNS 后 checkpoint 拒写的处理）；
- ``seg_text`` 的超步边界管理（agent commit 处清空）。
"""

from __future__ import annotations

from .context import StreamTurnCtx


async def run(ctx: StreamTurnCtx) -> None:
    raise NotImplementedError(
        "stages.toolloop.run — 批次 5-2 迁移目标（jt5-stages-base 仅骨架）"
    )
