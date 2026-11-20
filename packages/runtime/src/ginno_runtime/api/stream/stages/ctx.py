"""StreamTurnCtx — staged 引擎的跨阶段共享状态载体（批次 5-2）。

stages/ 四阶段（streamio / toolloop / permission / finalize）+ 编排器
（run.py）之间**只**经本对象传递 per-turn 可变状态；模块之间不读彼此的
模块级可变全局。字段名与 legacy ``engine._stream_graph`` 的局部变量一一
同名（复制式迁移，见 stages/README 的等价性对照表），变换规则只有一条：
``局部变量 → ctx.<同名属性>``。

两条纪律：

1. **engine 命名空间属性访问**：stages 内凡引用 engine.py 的模块级名字
   （``CHUNK_TIMEOUT_S``、``_RUNNING_TURNS``、``_session_meta_patch`` …）
   一律经 ``eng.<name>`` 属性读取（``from .. import engine as eng``），
   绝不做值快照 import——legacy 读的就是 engine 模块全局，monkeypatch 经
   ``_PatchMirrorModule`` 落在 engine 命名空间，属性访问保证 staged 与
   legacy 在同一时刻读到同一个（可能被 patch 过的）对象。
2. **全局注册表写入点位置语义**：``_RUNNING_TURNS`` / ``_RUNNING_TURNS_AT``
   / ``_TURN_STOP`` / ``_PENDING_KIND`` 的写入/弹出点与 legacy 逐点对应
   （见各写入处的「注册表写入点①②…」注释）。

``emit`` / ``safe_send`` 挂在 ctx 上（streamio 工厂创建）。闭包语义保持：
每次调用读 ctx 属性（不快照），与 legacy 闭包读函数局部变量的时机等价。
"""

from __future__ import annotations

from typing import Any, Callable


class StreamTurnCtx:
    """One turn segment's shared mutable state (legacy locals, same names)."""

    # ---- entry parameters ------------------------------------------------ #
    graph: Any
    config: dict
    input_state: dict | None
    command: Any  # langgraph.types.Command | None

    # ---- pre-try pre-init (legacy engine.py:126-166) --------------------- #
    saw_interrupt: bool
    ws_closed: bool
    seg_text: list[str]
    stop_waiter: Any
    _mods_t0: float
    session_id: str
    _turn_ctx_tokens: int
    _turn_ctx_seen: bool
    _turn_usage: dict
    _last_ai_id: str | None
    _cfg_conf: dict
    turn_id: str
    ui_turn_id: str
    stop_evt: Any  # asyncio.Event（try 内被共享 stop event 替换）

    # ---- try-body setup --------------------------------------------------- #
    slug: str
    agent_id: str
    usage_source: str
    stream: Any  # graph.astream(...) 三模式流
    _ka: Any  # keepalive task
    _pre_turn_todos: list
    _steer_user_arrived: bool
    _r_limit: int
    _r_warned: bool
    _agent_steps: int
    _found_meta: Any
    _is_subagent_session: bool

    # ---- stream-loop accumulators (legacy engine.py:368-432) -------------- #
    special_ids: dict[str, str]
    tool_args_by_id: dict[str, tuple[str, dict]]
    started_tool_ids: set[str]
    _SERVER_SEARCH_BUFFER: list[list[dict]]
    _ZAI_NARRATION_IDX: set
    _ZAI_MUTED: dict[Any, list[str]]
    _glm_started: set[str]
    _attach_filter: Any  # AttachTagStreamFilter
    _attach_sent: set[tuple[str, str, str]]
    _tool_name_by_id: dict[str, str]
    _partial_tool_args: dict[str, str]
    _emitted_tool_args: set[str]
    _emitted_tool_inputs: set[str]
    turn_text: list[str]

    # ---- io plumbing (streamio 工厂创建) ---------------------------------- #
    emit: Callable[..., str]
    safe_send: Callable[[str], Any]
    _coal: Any  # DeltaCoalescer

    def __init__(self, graph, config: dict, input_state: dict | None, command):
        # legacy engine.py:121-166 的预初始化（顺序/默认值逐一对应）。
        self.graph = graph
        self.config = config
        self.input_state = input_state
        self.command = command
        self.saw_interrupt = False
        self.ws_closed = False
        self.seg_text = []
        self.stop_waiter = None
        import time as _time

        self._mods_t0 = _time.monotonic()
        # legacy 161：session_id 先以 thread_id 预读，try 内重读一次（emit
        # 工厂创建前的那次赋值在 run.py 里，保持两读时序）。
        self.session_id = (config.get("configurable") or {}).get("thread_id", "")
        self._turn_ctx_tokens = 0
        self._turn_ctx_seen = False
        self._turn_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
        }
        self._last_ai_id = None
        self._cfg_conf = config.get("configurable") or {}
        self.turn_id = self._cfg_conf.get("turn_id") or ""
        self.ui_turn_id = self.turn_id
        # legacy 300（try 内）首个赋值、except 段（closeout.handle_turn_error
        # 的 steer-continue 判定）会读——异常落在 _setup_stream 之前时它必须
        # 已存在（预初始化防线，同 saw_interrupt/seg_text 一类）。
        self._steer_user_arrived = False
        # legacy 165 行的占位 Event（"a fresh one simply reads as no stop
        # requested"）：try 体内 setdefault 之前若抛异常，finally 的
        # ``stop_evt.is_set()`` 读到的是它——预初始化保证 finally 永不
        # 因未绑定名而掩盖原始失败（staged 用实例属性，同一防线）。
        import asyncio

        self.stop_evt = asyncio.Event()
