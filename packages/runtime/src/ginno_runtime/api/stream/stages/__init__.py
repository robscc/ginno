"""stages/ — ``_stream_graph`` 的四阶段 staged 实现（批次 5-2）。

结构（各文件职责见各 docstring）：

- ``ctx.py``       — ``StreamTurnCtx``：跨阶段唯一状态载体；
- ``streamio.py``  — 帧出口/合帧/token.delta 文本流/看门狗；
- ``toolloop.py``  — 工具调用帧/args 流/工具结果与错误路径；
- ``permission.py``— ``__interrupt__`` 停车（ask_user/permission/version）；
- ``finalize.py``  — 段末收尾/parked 退出/异常路径/finally 清理；
- ``run.py``       — 编排器（legacy 函数骨架）+ 分段对照表。

**唯一引擎路径**：批次 5 特征开关（GINNO_STREAM_ENGINE）与 legacy
``engine._stream_graph`` 原函数已于 dev-swap 真机观察期后删除；
``dispatch_stream_graph`` 是 turn.py 的唯一入口（保留函数名以维持
patch 面），直接进入 staged 管道。回滚走 git revert，不再走 env。
"""

from __future__ import annotations

from .run import stream_graph_staged


def dispatch_stream_graph(
    graph,
    config: dict,
    input_state: dict | None = None,
    command=None,
):
    """turn.py 的唯一入口（legacy 路已删，staged 为唯一引擎路径）。

    保留函数名与签名以维持 turn.py 的 import 面与测试 patch 面。
    """
    return stream_graph_staged(
        graph, config, input_state=input_state, command=command
    )
