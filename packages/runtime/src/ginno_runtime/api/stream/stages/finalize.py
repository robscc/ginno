"""阶段 4/4：turn 收尾与清理（批次 5-1 空壳）。

迁移目标（5-2 从 ``engine._stream_graph_legacy`` 复制式搬入）：
- interrupt / completed / error / stopped 四态出口（``saw_interrupt`` 路由、
  error 卡片 payload + ``_session_meta_patch`` 持久化、retry 按钮往返）；
- ``message.end`` 携带 per-turn 用量（``turn_usage`` / ``last_ai_id``）并落
  turns.jsonl；
- TODO 关联（``pre_turn_todos`` diff）、citations、auto-distill 节流；
- finally 清理：``_RUNNING_TURNS`` pop、keepalive/合帧 close、
  subagent reconcile、停车 turn 修复（``_stop_parked_turn`` /
  ``_heal_interrupted_turn``）、协作停止等待器的注销。
"""

from __future__ import annotations

from .context import StreamTurnCtx


async def run(ctx: StreamTurnCtx) -> None:
    raise NotImplementedError(
        "stages.finalize.run — 批次 5-2 迁移目标（jt5-stages-base 仅骨架）"
    )
