"""四阶段 turn 流水线（staged 引擎，批次 5-1 骨架 / 批次 5-2 迁移目标）。

这是 ``engine._stream_graph_legacy``（约 1650 行巨函数）的**复制式迁移
目标**：把它按耦合缝切成四个顺序阶段，每阶段一个 ``async def run(ctx)``,
共享 :class:`~ginno_runtime.api.stream.stages.context.StreamTurnCtx`。

开关语义（env ``GINNO_STREAM_ENGINE``，进程启动读一次缓存于
``engine._STREAM_ENGINE``，与 ``tools/code_mode_tools.py`` 的
``GINNO_CODE_MODE`` 同范式）：
- 缺省 / ``legacy`` / 任何无法识别的值 → 走 ``_stream_graph_legacy``，
  行为与拆分前**逐字节一致**（golden 快照 ``tests/goldens/engine_turn.txt``
  钉住，任何 diff = 契约漂移）；
- ``staged`` → 本包 ``run_staged_stream``。批次 5-2 迁移完成前各阶段
  ``raise NotImplementedError``——**fail-loud，绝不静默回落 legacy**，
  防止「以为在测新管道、实际跑旧路径」的假绿灯。

回退方式：桌面环境 unset ``GINNO_STREAM_ENGINE``（或显式 ``=legacy``）
并完全退出重开 Ginno 即回原引擎；开发热切换（``make dev-swap``）同理。
验收纪律（批次 5-2 起）：``tests/api/test_engine_golden.py`` 的 staged
参数路全绿 = 契约达成；legacy 路与快照的零漂移是**每一步迁移的前置
门槛**，不迁移完一个阶段绝不切默认值。

阶段切分（职责预划，5-2 按此逐段搬代码）：
1. :mod:`.permission` —— 段入口武装与 interrupt 应答：``begin_ask_budget``
   / permission ask 事件 / ``Command(resume=...)` 段重入的接续准备。
2. :mod:`.toolloop` —— agent 超步循环：tool_call 执行与事件、steer 吸收
   （含 ack 时序）、递归预算、瞬态错误 auto-retry / steer-continue 段重入。
3. :mod:`.streamio` —— 流出口：chunked stream、stall watchdog
   （``CHUNK_TIMEOUT_S``）、token/thinking delta 合帧、keepalive、
   ``safe_send`` 广播。
4. :mod:`.finalize` —— 收尾：interrupt/completed/error 三态出口、
   ``message.end`` + turn 用量、todos 关联、citations、auto-distill、
   finally 清理（``_RUNNING_TURNS`` pop / WAL / 停车修复）。
"""

from __future__ import annotations

from langgraph.types import Command

from .context import StreamTurnCtx
from . import finalize, permission, streamio, toolloop  # noqa: F401  (阶段模块)

__all__ = ["StreamTurnCtx", "run_staged_stream"]


async def run_staged_stream(
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """staged 引擎入口：构造 ctx 后依序驱动四阶段（签名对齐 _stream_graph）。

    批次 5-1：四阶段均为 NotImplementedError 占位——首个阶段即抛出，
    供 golden 双路参数化的 staged 断言钉住接线（开关 → 分叉 → 本入口
    → 阶段占位）。批次 5-2 起逐阶段填真身，staged 参数路的断言同步
    升级为与 legacy 相同的逐帧比对，绿灯即契约达成。
    """
    ctx = StreamTurnCtx(
        graph=graph,
        config=config,
        input_state=input_state,
        command=command,
    )
    await permission.run(ctx)
    await toolloop.run(ctx)
    await streamio.run(ctx)
    await finalize.run(ctx)
