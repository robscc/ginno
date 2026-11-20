"""stages/ — ``_stream_graph`` 的四阶段 staged 实现（批次 5-2）。

结构（各文件职责见各 docstring）：

- ``ctx.py``       — ``StreamTurnCtx``：跨阶段唯一状态载体；
- ``streamio.py``  — 帧出口/合帧/token.delta 文本流/看门狗；
- ``toolloop.py``  — 工具调用帧/args 流/工具结果与错误路径；
- ``permission.py``— ``__interrupt__`` 停车（ask_user/permission/version）；
- ``finalize.py``  — 段末收尾/parked 退出/异常路径/finally 清理；
- ``run.py``       — 编排器（legacy 函数骨架）+ 分段对照表。

开关：``GINNO_STREAM_ENGINE=staged`` 环境变量（每次调用读取，进程内可
monkeypatch）。默认（未设置/其它值）走 legacy ``engine._stream_graph``
（一行未动）——``dispatch_stream_graph`` 是 turn.py 的唯一分流入口。
"""

from __future__ import annotations

import os

from ..engine import _stream_graph as _legacy_stream_graph
from .run import stream_graph_staged

ENV_KEY = "GINNO_STREAM_ENGINE"
ENV_STAGED = "staged"


def staged_enabled() -> bool:
    """GINNO_STREAM_ENGINE=staged（大小写/空白宽容，每次现读 env）。"""
    return os.environ.get(ENV_KEY, "").strip().lower() == ENV_STAGED


def dispatch_stream_graph(
    graph,
    config: dict,
    input_state: dict | None = None,
    command=None,
):
    """turn.py 的唯一入口：按 ``GINNO_STREAM_ENGINE`` 分流。

    legacy 路持 engine._stream_graph 的原引用（与拆分前 turn.py 的
    ``from .engine import _stream_graph`` 同一值，patch 面不变）。
    """
    if staged_enabled():
        return stream_graph_staged(
            graph, config, input_state=input_state, command=command
        )
    return _legacy_stream_graph(
        graph, config, input_state=input_state, command=command
    )
