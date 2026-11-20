"""stages 引擎的 AsyncExitStack 资源登记验收（批次 5-3）。

两大主张各有一组测试钉死：

1. **异常路径资源全清**（根治三次泄漏事故的结构病）：stub graph 在流
   中途抛异常，turn 走 except 分支（error 卡收口）后，注册表
   ``_RUNNING_TURNS``/``_RUNNING_TURNS_AT``/``_TURN_STOP``/``_PENDING_KIND``
   必须全部回空、ask_budget 的 contextvar 必须还原——清理由栈的 LIFO
   弹出保证，不依赖 finally 段的执行完整性。
2. **默认 staged + 显式回退**：未设 env 走 staged（stages/ 四阶段），
   ``GINNO_STREAM_ENGINE=legacy`` 走 legacy ``engine._stream_graph``
   原函数（回退路持续可用）。

栈条目清单（E1-E6，登记点与释放函数见 stages/finalize.py 文件头表）：
E1 ask_budget token reset / E2 条件收尾组（注册表④+终态钩子）/
E3 _TURN_STOP.pop / E4 stop_waiter.cancel / E5 keepalive.cancel /
E6 coal.close。压栈序 E1→E6，弹出序（LIFO）= legacy finally 逐行清理序。
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import server, server_shared
from ginno_runtime.api.stream import engine
from ginno_runtime.api.stream.stages import (
    dispatch_stream_graph,
    staged_enabled,
    stream_graph_staged,
)
from ginno_runtime.testing.fake_model import script
from ginno_runtime.tools import ask_tools


class _BoomGraph:
    """astream 在首个 chunk 处抛异常的 stub graph。

    资源获取段（_setup_identity/_setup_io/stop_waiter）全部完成后才爆
    ——模拟「三次泄漏事故」的时序：异常落在资源已登记、正常收尾未跑的
    窗口内。"""

    def __init__(self) -> None:
        self.consumed = False

    def astream(self, *_a, **_k):
        async def _gen():
            self.consumed = True
            raise RuntimeError("boom-mid-turn")
            yield  # pragma: no cover — 生成器语法需要

        return _gen()


def _turn_cfg(session_id: str) -> dict:
    session = server._SESSIONS[session_id]
    return {
        "configurable": {
            "thread_id": session_id,
            "project_slug": session.get("project_slug") or "default",
            "agent_id": session.get("agent_id"),
            "turn_id": "turn-exitstack-1",
            "user_text": "触发异常路径",
            "usage_source": "chat",
        }
    }


def _assert_registries_empty(session_id: str) -> None:
    assert session_id not in server_shared._RUNNING_TURNS
    assert session_id not in server_shared._RUNNING_TURNS_AT
    assert session_id not in server_shared._TURN_STOP
    assert session_id not in server_shared._PENDING_KIND


def test_staged_default_and_legacy_fallback(monkeypatch):
    """默认翻转：未设 env = staged；=legacy 回退 legacy；=staged 仍 staged。"""
    monkeypatch.delenv("GINNO_STREAM_ENGINE", raising=False)
    assert staged_enabled() is True
    monkeypatch.setenv("GINNO_STREAM_ENGINE", "legacy")
    assert staged_enabled() is False
    monkeypatch.setenv("GINNO_STREAM_ENGINE", "staged")
    assert staged_enabled() is True
    # 大小写/空白宽容
    monkeypatch.setenv("GINNO_STREAM_ENGINE", "  LEGACY ")
    assert staged_enabled() is False


def test_dispatch_routes_to_staged_by_default(monkeypatch):
    """分流点：未设 env 时 dispatch 返回 staged 协程对象（非 legacy 函数）。"""
    monkeypatch.delenv("GINNO_STREAM_ENGINE", raising=False)

    async def _noop():
        return None

    class _G:  # dispatch 只做分流不消费 graph
        pass

    calls = []

    def _fake_staged(graph, config, input_state=None, command=None):
        calls.append("staged")
        return _noop()

    def _fake_legacy(graph, config, input_state=None, command=None):
        calls.append("legacy")
        return _noop()

    import ginno_runtime.api.stream.stages as stages_pkg

    monkeypatch.setattr(stages_pkg, "stream_graph_staged", _fake_staged)
    monkeypatch.setattr(stages_pkg, "_legacy_stream_graph", _fake_legacy)
    asyncio.run(dispatch_stream_graph(_G(), {}, input_state=None, command=None))
    assert calls == ["staged"]
    monkeypatch.setenv("GINNO_STREAM_ENGINE", "legacy")
    asyncio.run(dispatch_stream_graph(_G(), {}, input_state=None, command=None))
    assert calls == ["staged", "legacy"]


def test_exit_stack_clears_all_turn_resources_on_error(
    client, create_session, monkeypatch
):
    """异常路径全清：stub graph 中途爆 → 栈弹出后注册表回空 + 预算还原。

    这是批次 5-3 的核心验收：legacy finally 若在异常早段断链（任一未包
    try 的调用抛出），其后的注册表清理会被跳过；栈化后每条目独立执行，
    互不阻断。
    """
    monkeypatch.delenv("GINNO_STREAM_ENGINE", raising=False)
    # 防止 handle_turn_error 把 RuntimeError 判成 transient 触发 auto-retry
    from ginno_runtime.api.stream import engine as eng_mod

    monkeypatch.setattr(eng_mod, "_is_transient_model_error", lambda e: False)

    sid = create_session([script(text="不会到达")])
    boom = _BoomGraph()

    async def _drive() -> object:
        # 同一协程/task 的 context 链：turn 结束后读 _BUDGET 即 reset 后值。
        before = ask_tools._BUDGET.get()
        await stream_graph_staged(boom, _turn_cfg(sid), input_state=None, command=None)
        return ask_tools._BUDGET.get(), before

    after, before = asyncio.run(_drive())

    assert boom.consumed, "stub graph 必须真的被消费（异常发生在资源登记后）"
    _assert_registries_empty(sid)
    # E1：ask_budget token reset——begin 设置的 {"n": 0} 不得残留
    assert after == before


def test_exit_stack_releases_when_setup_stream_raises(client, create_session, monkeypatch):
    """更早的异常窗口：_setup_stream 抛出（coal/ka 已登记、主循环未进）。"""
    monkeypatch.delenv("GINNO_STREAM_ENGINE", raising=False)
    from ginno_runtime.api.stream import engine as eng_mod
    from ginno_runtime.api.stream.stages import run as run_mod

    monkeypatch.setattr(eng_mod, "_is_transient_model_error", lambda e: False)
    sid = create_session([script(text="不会到达")])

    def _boom_setup_stream(ctx):
        raise RuntimeError("boom-in-setup-stream")

    monkeypatch.setattr(run_mod, "_setup_stream", _boom_setup_stream)

    async def _drive() -> object:
        before = ask_tools._BUDGET.get()
        try:
            await stream_graph_staged(
                _BoomGraph(), _turn_cfg(sid), input_state=None, command=None
            )
        except RuntimeError:
            pass
        return ask_tools._BUDGET.get(), before

    after, before = asyncio.run(_drive())
    _assert_registries_empty(sid)
    assert after == before


def test_exit_stack_keeps_registration_when_parked(client, create_session, monkeypatch):
    """parked 语义不回退：interrupt 停车时注册表保留（resume 前仍 running）。

    E2 条件收尾组内 ``not saw_interrupt`` 分支语义的护栏——栈化不得把
    parked turn 的注册表也弹掉（resume 段会重新登记并在其 E2 里清理）。
    """
    monkeypatch.delenv("GINNO_STREAM_ENGINE", raising=False)
    sid = create_session([script(text="x")])

    from ginno_runtime.api.stream.stages.ctx import StreamTurnCtx
    from ginno_runtime.api.stream.stages.finalize import _release_turn_registration

    server_shared._RUNNING_TURNS[sid] = "turn-parked"
    server_shared._RUNNING_TURNS_AT[sid] = 0.0
    ctx = StreamTurnCtx(None, _turn_cfg(sid), None, None)
    ctx.session_id = sid
    ctx.turn_id = "turn-parked"
    ctx.ui_turn_id = "turn-parked"
    ctx.saw_interrupt = True

    async def _drive():
        await _release_turn_registration(ctx)

    asyncio.run(_drive())
    # parked：注册表保留，交给 resume 段的 E2 弹出清理
    assert server_shared._RUNNING_TURNS.get(sid) == "turn-parked"
    assert sid in server_shared._RUNNING_TURNS_AT
    # 清场（本测试直接驱动了条件组，不经过完整栈）
    server_shared._RUNNING_TURNS.pop(sid, None)
    server_shared._RUNNING_TURNS_AT.pop(sid, None)


def test_legacy_engine_function_untouched():
    """旧路原函数仍在：engine._stream_graph 一行未动（删旧路是独立后续 PR）。"""
    import inspect

    src = inspect.getsource(engine._stream_graph)
    # 栈化只发生在 stages/；legacy 函数体内不得出现 AsyncExitStack
    assert "AsyncExitStack" not in src
    assert "stream_graph_staged" not in src
