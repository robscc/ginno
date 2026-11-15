"""Unit tests for 2026-11-15 内存基线治理（性能审计 P0）。

覆盖三处加帽逻辑：
1. _TUPLE_CACHE 64MB 字节预算（旧版按条数不按字节，大会话可吃数百 MB）；
2. _SESSIONS LRU cap=16 且逐出跳过活跃会话（running/parked/live WS）；
3. RUN_CACHE 插入序帽 32（旧版只写不清）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from ginno_runtime import server_shared as shared
from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.tools.workflow_tools import RUN_CACHE, RUN_CACHE_CAP, _prune_run_cache

pytestmark = pytest.mark.unit


def _cfg(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def _reset_tuple_cache():
    with FileCheckpointer._TUPLE_CACHE_LOCK:
        FileCheckpointer._TUPLE_CACHE.clear()
        FileCheckpointer._TUPLE_CACHE_BYTES.clear()
        FileCheckpointer._TUPLE_CACHE_TOTAL = 0


def test_tuple_cache_byte_budget_evicts_lru(isolated_home):
    _reset_tuple_cache()
    cp = FileCheckpointer(project_slug="p")
    big = "x" * (25 * 1024 * 1024)  # 25MB/会话 ×3 = 75MB > 64MB 预算
    for sid in ("s1", "s2", "s3"):
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"]["messages"] = [HumanMessage(content=big)]
        cp.put(_cfg(sid), checkpoint, {"source": "input", "step": 0, "writes": {}}, {})
        assert cp._cached_head_tuple(_cfg(sid)) is not None
    with FileCheckpointer._TUPLE_CACHE_LOCK:
        total = FileCheckpointer._TUPLE_CACHE_TOTAL
        keys = set(FileCheckpointer._TUPLE_CACHE.keys())
    assert total <= FileCheckpointer._TUPLE_CACHE_BUDGET, f"byte budget exceeded: {total}"
    assert ("p", "s1") not in keys, "oldest entry must be evicted first"
    assert ("p", "s3") in keys, "newest entry must survive"
    _reset_tuple_cache()


def test_tuple_cache_small_entries_keep_count_cap(isolated_home):
    _reset_tuple_cache()
    cp = FileCheckpointer(project_slug="p")
    for i in range(10):
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"]["messages"] = [HumanMessage(content=f"m{i}")]
        cp.put(_cfg(f"s{i}"), checkpoint, {"source": "input", "step": 0, "writes": {}}, {})
        cp._cached_head_tuple(_cfg(f"s{i}"))
    with FileCheckpointer._TUPLE_CACHE_LOCK:
        assert len(FileCheckpointer._TUPLE_CACHE) <= 8
        assert FileCheckpointer._TUPLE_CACHE_TOTAL == sum(
            FileCheckpointer._TUPLE_CACHE_BYTES.values()
        ), "byte ledger must stay consistent with entries"
    _reset_tuple_cache()


def test_session_registry_lru_skips_active():
    shared._SESSIONS.clear()
    for i in range(20):
        shared.session_registry_put(f"s{i}", {"session_id": f"s{i}"})
    assert len(shared._SESSIONS) == shared.SESSION_REGISTRY_CAP
    assert "s0" not in shared._SESSIONS, "oldest idle evicted"
    assert "s19" in shared._SESSIONS

    # 活跃会话不被逐出：running turn / parked resume / live WS 三种标记
    shared._SESSIONS.clear()
    for i in range(16):
        shared.session_registry_put(f"a{i}", {"session_id": f"a{i}"})
    shared._RUNNING_TURNS["a0"] = "turn-1"
    shared._PENDING_RESUME.add("a1")
    shared._SESSION_WS["a2"] = []
    for i in range(10):
        shared.session_registry_put(f"b{i}", {"session_id": f"b{i}"})
    assert "a0" in shared._SESSIONS, "running session must survive eviction"
    assert "a1" in shared._SESSIONS, "parked session must survive eviction"
    assert "a2" in shared._SESSIONS, "live-WS session must survive eviction"
    shared._RUNNING_TURNS.pop("a0", None)
    shared._PENDING_RESUME.discard("a1")
    shared._SESSION_WS.pop("a2", None)
    shared._SESSIONS.clear()


def test_run_cache_cap():
    RUN_CACHE.clear()
    for i in range(RUN_CACHE_CAP + 10):
        RUN_CACHE[f"r{i}"] = {"id": f"r{i}"}
        _prune_run_cache()
    assert len(RUN_CACHE) == RUN_CACHE_CAP
    assert "r0" not in RUN_CACHE
    assert f"r{RUN_CACHE_CAP + 9}" in RUN_CACHE
    RUN_CACHE.clear()

# ── 线程池右 sizing（2026-11-15 补：原 4 个用例漏了这一项，bug 因此溜过）──
#
# 原写法 anyio.current_default_thread_limiter() 在 anyio 4.x 顶层不存在，
# 每次启动抛 AttributeError 被 except 吞掉，且同一 try 块里的
# set_default_executor 也一起没执行——两项优化全部落空。
# 下面三个用例分别钉住：API 路径、两项真落地、两项互相独立。


def test_anyio_thread_limiter_api_path():
    """回归钉：limiter 在 anyio.to_thread 下，不在顶层。

    不要"顺手改成" anyio.current_default_thread_limiter()——那是 4.x 的
    错误路径，会让整个右 sizing 静默失效（2026-11-15 实录）。
    """
    import anyio
    import anyio.to_thread

    assert hasattr(anyio.to_thread, "current_default_thread_limiter")
    assert not hasattr(anyio, "current_default_thread_limiter")


@pytest.mark.asyncio
async def test_lifespan_applies_both_thread_pool_settings(isolated_home):
    """跑真 lifespan，断言两项优化都真落地（不是只看不报错）。"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import anyio.to_thread

    from ginno_runtime.server import app, lifespan

    async with lifespan(app):
        # ① anyio limiter 40 → 12
        assert anyio.to_thread.current_default_thread_limiter().total_tokens == 12
        # ② asyncio 默认 executor 换成 ginno-io 8 线程
        ex = asyncio.get_running_loop()._default_executor
        assert isinstance(ex, ThreadPoolExecutor)
        assert ex._max_workers == 8
        assert ex._thread_name_prefix == "ginno-io"


@pytest.mark.asyncio
async def test_thread_pool_settings_fail_independently(isolated_home, monkeypatch):
    """一项失败不得连带另一项（原共用 try 块的结构性缺陷）。"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import anyio.to_thread

    from ginno_runtime import server as srv

    def _boom():
        raise RuntimeError("limiter unavailable")

    monkeypatch.setattr(anyio.to_thread, "current_default_thread_limiter", _boom)
    async with srv.lifespan(srv.app):
        # limiter 炸了，但 executor 仍必须被设上
        ex = asyncio.get_running_loop()._default_executor
        assert isinstance(ex, ThreadPoolExecutor)
        assert ex._max_workers == 8
