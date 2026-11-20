"""StreamTurnCtx —— staged 管道的 turn 级状态载体（批次 5-1，纯清单无行为）。

字段逐一盘点自 ``engine._stream_graph_legacy`` 入口段的局部状态
（``engine.py`` ``_stream_graph_legacy`` 定义起约前 180 行：预初始化区 +
try 块开头的登记区）。本类在批次 5-1 **只列不实现**：四阶段空壳
（permission/toolloop/streamio/finalize）以它为唯一形参，5-2 复制式迁移
时各阶段在 ctx 上读写这些槽位，替代巨函数闭包里的散落局部变量。

字段分两组：
- **段输入**（invoke/resume 的入参，run_staged_stream 构造时灌入）；
- **turn 级可变状态**（原局部变量 → ctx 槽位，注释标注原变量名与语义）。

命名纪律：字段名去掉原局部变量的 ``_`` 前缀（那是巨函数的私有约定），
其余保持可 grep 对照；原行号见各字段注释（行号基于 jt5-stages-base 切点
35c8c12 的 engine.py）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from langgraph.types import Command


def _fresh_turn_usage() -> dict:
    """per-turn 用量累计的零值形态（原局部 ``_turn_usage`` 初值，五键）。"""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }


@dataclass
class StreamTurnCtx:
    """一个 turn 段（invoke 或 resume）在四阶段间共享的全部状态。"""

    # ── 段输入（run_staged_stream 由分叉入口的入参灌入）──────────────────
    graph: Any = None
    """LangGraph 编排图（原形参 ``graph``）。"""
    config: dict = field(default_factory=dict)
    """RunnableConfig（原形参 ``config``；configurable 内含 thread_id/
    turn_id/project_slug/agent_id 等）。"""
    input_state: dict | None = None
    """invoke 段的初始 state（原形参 ``input_state``；resume 段为 None）。"""
    command: Command | None = None
    """resume 段的 ``Command(resume=...)``（原形参 ``command``）。"""

    # ── turn 标识与会话定位（engine.py 预初始化区 + try 开头重取）────────
    session_id: str = ""
    """configurable.thread_id（原局部 ``session_id``，try 内重取）。"""
    slug: str = "default"
    """项目 slug（原局部 ``slug``）。"""
    agent_id: str = ""
    """执行 agent（原局部 ``agent_id``）。"""
    usage_source: str = "chat"
    """用量归因标签（原局部 ``usage_source``：chat/goal/schedule/...）。"""
    turn_id: str = ""
    """**执行** turn id（原局部 ``turn_id``；auto-retry/段重入跑派生 id
    ``…--a1``/``…--s1-xxxx``，因为原始 id 已进 ABANDONED_TURNS）。"""
    ui_turn_id: str = ""
    """**客户端**稳定 turn id（原局部 ``ui_turn_id``；事件/last_error/
    _RUNNING_TURNS 全走它，重试按钮往返闭合）。"""
    cfg_conf: dict = field(default_factory=dict)
    """config["configurable"] 直引用（原局部 ``_cfg_conf``；注意 try 内会
    回写 turn_id 进去——ctx 持有的是同一 dict，非拷贝）。"""

    # ── 停止 / 中断 / ws 通路（预初始化区 + try 内登记）───────────────────
    saw_interrupt: bool = False
    """本轮是否停在 graph interrupt（原局部 ``saw_interrupt``：收尾时据此
    走 interrupt 分支而非 completed/error）。"""
    ws_closed: bool = False
    """最近一次广播是否发现零活 socket（原局部 ``ws_closed``；诊断用，
    **从不锁存**——重连即恢复投递，2026-08-05 事件）。"""
    stop_waiter: Any = None
    """协作停止等待器（原局部 ``stop_waiter``）。"""
    stop_evt: asyncio.Event | None = None
    """协作停止信号（原局部 ``stop_evt``；try 内 ``_TURN_STOP.setdefault``
    取会话共享事件——WS 环预先武装的事件不可被覆盖）。"""
    seg_text: list[str] = field(default_factory=list)
    """**当前 agent 超步**的未提交文本（原局部 ``seg_text``：每次 agent
    commit 清空；用户 stop 时它被持久化为部分 AIMessage）。"""
    steer_user_arrived: bool = False
    """用户（非系统 recursion-warn）steer 是否已到达（原局部
    ``_steer_user_arrived``：用户 steer 合法加预算，系统的绝不加）。"""

    # ── 计时 / 用量 / 上下文口径（预初始化区）─────────────────────────────
    mods_t0: float = field(default_factory=time.monotonic)
    """段起点单调钟（原局部 ``_mods_t0``：mods turn.complete 的 durationMs，
    turn.start tap 处重置、resume 段保留段入口戳）。"""
    ask_budget_armed: bool = False
    """``begin_ask_budget()`` 是否已在本段武装（原函数体第一行调用——每段
    （invoke + 每次 resume）各自独立 task context，预算不跨段泄漏）。"""
    turn_ctx_tokens: int = 0
    """ContextRing 分子的 turn 级平滑基线（原局部 ``_turn_ctx_tokens``：
    首个 usage 定基线、步间取 max、漏报轮保旧值——修占比锯齿）。"""
    turn_ctx_seen: bool = False
    """本 turn 是否已见过 usage（原局部 ``_turn_ctx_seen``）。"""
    turn_usage: dict = field(default_factory=_fresh_turn_usage)
    """per-turn 用量累计（原局部 ``_turn_usage``，五键；completed 收尾随
    message.end 下发并落 turns.jsonl）。"""
    last_ai_id: str | None = None
    """最后一条 assistant 消息 id（原局部 ``_last_ai_id``：turn-meta
    「右下角耗时+token」投影用）。"""

    # ── 流出口 / 后台伴生任务（try 内建立的闭包与 task）───────────────────
    emit: Callable[..., str] | None = None
    """帧出口闭包（原闭包 ``emit``：vibe_island tap + frame_session 戳 +
    ``_ev``——turn 每帧带 trace id 的收口点）。"""
    safe_send: Callable[..., Any] | None = None
    """会话级广播闭包（原闭包 ``safe_send``：遍历 ``_SESSION_WS`` 逐 socket
    试发、剪死连接、喂 event_sink——keepalive/合帧/事件共用出口）。"""
    coalescer: Any = None
    """token.delta/thinking.delta 合帧器（原局部 ``_coal``：
    ``DeltaCoalescer``，33ms 窗口；非 delta 事件前 flush 保序）。"""
    keepalive_task: asyncio.Task | None = None
    """15s 保活协程（原局部 ``_ka``：前端 watchdog 45s 线内重置 lastSeen）。"""
    pre_turn_todos: list = field(default_factory=list)
    """turn 前 TODO 快照（原局部 ``_pre_turn_todos``：turn 后 diff 出本
    turn 创建/触碰的条目并自动关联本会话）。"""

    # ── code 图片统一化（image-emit-universal）────────────────────────────
    baseline_image_mtimes: dict = field(default_factory=dict)
    """turn 开始时 registry 里本会话已有 image 条目的 ``{id: mtime}``
    快照的**拷贝**（原始快照在 ``server_shared._TURN_IMAGE_BASELINE``，
    agent_node lift 读那份——本拷贝被 stream 层在 emit 后拉平，故两者
    必须分离）。stream 兜底 diff：新 id 或 mtime 更新 → 补播。"""
    emitted_image_ids: set = field(default_factory=set)
    """本 turn 已播过 ``image.emit`` 的 image id（bash marker 分支与通用
    兜底共用，防同图双播）。"""
