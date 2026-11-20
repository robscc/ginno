"""阶段 1/4：段入口武装与 interrupt 应答（批次 5-1 空壳）。

迁移目标（5-2 从 ``engine._stream_graph_legacy`` 复制式搬入）：
- ``begin_ask_budget()`` 段武装（每段独立 task context，不跨段泄漏）；
- turn_id / ui_turn_id 派生与 ``_cfg_conf`` 回写、``_ensure_turn_log``；
- ``_RUNNING_TURNS`` 登记 + ``_RUNNING_TURNS_AT`` 心跳戳（turn_gate_busy
  的过期自愈锚点）；
- lastwords WAL ``note_turn_start``、``_TURN_STOP.setdefault`` 停止信号；
- graph interrupt 的 permission ask 事件与 ``Command(resume=...)`` 段重入
  的接续准备。
"""

from __future__ import annotations

from .context import StreamTurnCtx


async def run(ctx: StreamTurnCtx) -> None:
    raise NotImplementedError(
        "stages.permission.run — 批次 5-2 迁移目标（jt5-stages-base 仅骨架）"
    )
