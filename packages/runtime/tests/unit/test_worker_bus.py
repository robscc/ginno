"""Worker 协议单测（docs/worker-protocol-design.md §10 六幕的机制层）。

覆盖：
* 状态投影（subagent/delegation/main 各型 + 活跃信号）
* 花名册（过滤 + 跨项目 + 歧义解析）
* assign 三态回执：忙 steer（origin=worker 保留待遇）/ 闲 wake（出口码
  映射 absorbed/queued/refused）/ 拒绝（终态、非 assignable、空 goal）
* watch 注册：meta subscribers、幂等刷新、终态短路、上限
* notify_settled：血缘去重、一次性移除、digest 保留
* digest 节流 + on_turn_settled_hook 快查表
* reconcile_subscriptions 启动补发
* steer_keep_channel 的 worker 保留

重管道（_wake_parent_turn / _inject_result / WS push）monkeypatch——
协议层逻辑真实。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths, server_shared
from ginno_runtime import subagent_scheduler as sched
from ginno_runtime.session_meta import _find_meta, _session_meta_list, _session_meta_upsert
from ginno_runtime import worker_bus
from ginno_runtime.worker_bus import deliver as wd
from ginno_runtime.worker_bus import watch as ww
from ginno_runtime.worker_bus.tools import build_worker_tools

pytestmark = pytest.mark.unit

SLUG = "default"


@pytest.fixture(autouse=True)
def _clean_state():
    regs = (
        server_shared._SESSIONS,
        server_shared._STEER_STASH,
        server_shared._RUNNING_TURNS,
        server_shared._MANAGED_TURNS,
        server_shared._PENDING_RESUME,
        server_shared._TURN_STOP,
        server_shared._TURN_TASKS,
        sched._PENDING_INJECTIONS,
        sched._INJECTION_WAKING,
        ww._WATCHED,
    )
    for r in regs:
        r.clear()
    yield
    for r in regs:
        r.clear()


def _put_meta(slug: str, meta: dict) -> None:
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    _session_meta_upsert(slug, meta)


def _worker_meta(
    sid: str,
    status: str = "running",
    goal: str = "分析构建日志",
    title: str | None = None,
    parent: str | None = None,
    subscribers: list | None = None,
) -> dict:
    m: dict = {
        "id": sid,
        "title": title or f"t-{sid}",
        "type": "subagent",
        "parent_session_id": parent,
        "depth": 0 if parent else 1,
        "created": time.time(),
        "updated": time.time(),
        "subagent": {
            "goal": goal,
            "status": status,
            "result_summary": "结论：三条失败均为同一根因" if status == "done" else "",
        },
    }
    if subscribers is not None:
        m["subscribers"] = subscribers
    return m


def _main_meta(sid: str, title: str | None = None, *, legacy: bool = False) -> dict:
    """2026-10-29 起创建即写 type="main"（主动标注）；legacy=True 还原
    迁移前的 null-type 形状（_mtype 兜底路径的覆盖对象）。"""
    m = {
        "id": sid,
        "title": title or f"main-{sid}",
        "created": time.time(),
        "updated": time.time(),
    }
    if not legacy:
        m["type"] = "main"
    return m


# ----------------------------- 状态投影 ------------------------------------- #


def test_project_status_subagent_lifecycle():
    _put_meta(SLUG, _worker_meta("W1", status="done"))
    _put_meta(SLUG, _worker_meta("W2", status="running"))
    assert worker_bus.project_status("W1") == "done"
    # meta running + 无活跃信号 → unknown（进程内没这个会话）
    assert worker_bus.project_status("W2") == "unknown"
    # 活跃信号覆盖：RUNNING_TURNS → working；PENDING_RESUME → waiting
    server_shared._RUNNING_TURNS["W2"] = "turn-1"
    assert worker_bus.project_status("W2") == "working"
    del server_shared._RUNNING_TURNS["W2"]
    server_shared._PENDING_RESUME.add("W2")
    assert worker_bus.project_status("W2") == "waiting"
    server_shared._PENDING_RESUME.discard("W2")
    server_shared._SESSIONS["W2"] = {"session_id": "W2"}
    assert worker_bus.project_status("W2") == "idle"
    assert worker_bus.project_status("nope") == "missing"


def test_project_status_delegation_and_main():
    _put_meta(SLUG, {
        "id": "D1", "title": "d1", "type": "delegation",
        "stop_reason": "success", "created": 1, "updated": 2,
    })
    _put_meta(SLUG, {
        "id": "D2", "title": "d2", "type": "delegation",
        "stop_reason": "error", "created": 1, "updated": 2,
    })
    _put_meta(SLUG, _main_meta("M1"))
    assert worker_bus.project_status("D1") == "done"
    assert worker_bus.project_status("D2") == "failed"
    # main 会话已放开可派活（2026-10-29）：resolve 通过；channel 仍拒
    _, meta, err = worker_bus.resolve_target("M1")
    assert not err and meta is not None
    _put_meta(SLUG, {"id": "C1", "title": "c1", "type": "channel",
                     "created": 1, "updated": 2})
    _, _, err = worker_bus.resolve_target("C1")
    assert err and "not assignable" in err


def test_watch_main_digest_only():
    """main 无终态：terminal 订阅拒绝并引导 digest；digest 正常注册。"""
    _put_meta(SLUG, _main_meta("M1", title="AMS 迁移"))
    r = asyncio.run(worker_bus.watch_worker("S1", "M1", "terminal"))
    assert not r.get("ok") and "digest" in r.get("error", "")
    r = asyncio.run(worker_bus.watch_worker("S1", "M1", "digest"))
    assert r.get("ok") and r.get("want") == "digest"
    subs = (_find_meta("M1")[0].get("subscribers")) or []
    assert [s["want"] for s in subs] == ["digest"]


def test_main_explicit_type_and_legacy_null_both_assignable():
    """主动标注（type="main"）与迁移前旧形状（type 缺省）都是合法 worker；
    归一后花名册 type 列统一显示 main。"""
    _put_meta(SLUG, _main_meta("M2", title="新标注"))
    _put_meta(SLUG, _main_meta("M3", title="旧形状", legacy=True))
    for sid in ("M2", "M3"):
        _, meta, err = worker_bus.resolve_target(sid)
        assert not err and meta is not None
    rows = {r["session_id"]: r for r in worker_bus.list_worker_rows(SLUG)}
    assert rows["M2"]["type"] == "main" and rows["M3"]["type"] == "main"


def test_backfill_session_types():
    """启动迁移：null type 补写 "main"，特殊类型不动，幂等零写入。"""
    from ginno_runtime.session_meta import backfill_session_types

    _put_meta(SLUG, _main_meta("M4", legacy=True))
    _put_meta(SLUG, _worker_meta("W5"))  # subagent 不动
    n = backfill_session_types()
    assert n >= 1
    got = {m["id"]: m for m in _session_meta_list(SLUG)}
    assert got["M4"]["type"] == "main"
    assert got["W5"]["type"] == "subagent"
    # 幂等：收敛后再跑补写数为 0
    assert backfill_session_types() == 0


def test_worker_flag_optout_is_main_only():
    """「接活」开关（扇形菜单标注，meta.worker）：main 缺省接活、False 退出
    花名册与派活；task-typed 不受开关控制（天生 worker）。"""
    _put_meta(SLUG, _main_meta("M5", title="接活中"))
    _put_meta(SLUG, {**_main_meta("M6", title="已退休"), "worker": False})
    _put_meta(SLUG, {**_worker_meta("W6", status="running"), "worker": False})
    rows = {r["session_id"] for r in worker_bus.list_worker_rows(SLUG)}
    assert "M5" in rows and "M6" not in rows
    assert "W6" in rows  # task-typed 开关无效
    _, _, err = worker_bus.resolve_target("M6")
    assert err and "not accepting" in err
    sid, _, err = worker_bus.resolve_target("W6")
    assert sid == "W6" and not err


# ----------------------------- 花名册 --------------------------------------- #


def test_list_worker_rows_filters_and_cross_project():
    _put_meta(SLUG, _worker_meta("W1", status="done", title="日志分析"))
    _put_meta(SLUG, _worker_meta("W2", status="running", title="编译修复"))
    _put_meta("other", _worker_meta("X1", status="idle", title="跨项目工人"))
    _put_meta(SLUG, _main_meta("M9", title="AMS 迁移"))
    _put_meta(SLUG, {"id": "C1", "title": "企微", "type": "channel",
                     "created": 1, "updated": 3})
    # 默认全局（项目概念弱化：寻址跨项目，花名册同口径，行内标注 project）；
    # main 进花名册（type 归一）；channel 不进（实时对话面不可派活）
    rows = worker_bus.list_worker_rows(SLUG)
    assert {r["session_id"] for r in rows} == {"W1", "W2", "X1", "M9"}
    proj = {r["session_id"]: r["project"] for r in rows}
    assert proj["X1"] == "other" and proj["W1"] == SLUG
    assert next(r for r in rows if r["session_id"] == "M9")["type"] == "main"
    done_row = next(r for r in rows if r["session_id"] == "W1")
    assert done_row["result_summary"].startswith("结论")  # done 才带摘要
    assert all(not r["result_summary"] for r in rows if r["session_id"] == "W2")
    assert {r["session_id"] for r in worker_bus.list_worker_rows(SLUG, status_filter="done")} == {"W1"}
    assert {r["session_id"] for r in worker_bus.list_worker_rows(SLUG, query="编译")} == {"W2"}
    # 收窄回单项目
    assert {r["session_id"] for r in worker_bus.list_worker_rows(SLUG, all_projects=False)} == {"W1", "W2", "M9"}


def test_resolve_target_id_title_ambiguous():
    _put_meta(SLUG, _worker_meta("W1", title="日志分析"))
    _put_meta(SLUG, _worker_meta("W2", title="日志分析二"))
    sid, meta, err = worker_bus.resolve_target("W1")
    assert sid == "W1" and not err
    sid, meta, err = worker_bus.resolve_target("日志分析二")
    assert sid == "W2" and not err
    # 两个都含「日志分析」→ 歧义，列出候选
    sid, meta, err = worker_bus.resolve_target("日志分析")
    assert sid == "" and "ambiguous" in err and "W1" in err and "W2" in err
    sid, _, err = worker_bus.resolve_target("不存在的")
    assert sid == "" and "no worker" in err


# ----------------------------- assign 三态 ---------------------------------- #


async def test_assign_busy_targets_steer_queue():
    _put_meta(SLUG, _worker_meta("W1", status="running"))
    server_shared._RUNNING_TURNS["W1"] = "turn-1"
    r = await worker_bus.assign_to_worker("SENDER", "W1", "继续修复测试", constraints="只读")
    assert r["receipt"] == "queued"
    (entry,) = server_shared._STEER_STASH["W1"]
    assert entry["origin"] == "worker"
    assert entry["steer_id"].startswith("worker-")
    assert "ginno_worker_msg" in entry["extra_kwargs"]
    assert entry["extra_kwargs"]["ginno_worker_msg"]["from"] == "SENDER"
    assert "<ginno_worker_assign" in entry["text"]
    assert "<goal>继续修复测试</goal>" in entry["text"]
    assert "<constraints>只读</constraints>" in entry["text"]


async def test_assign_idle_maps_wake_outcomes(monkeypatch):
    _put_meta(SLUG, _worker_meta("W1", status="waiting"))
    seen: dict = {}

    async def fake_wake(tid, text, extra_kwargs, steer_origin=""):
        seen.update(tid=tid, text=text, origin=steer_origin)
        return seen.pop("outcome", "ran")

    monkeypatch.setattr(wd, "_wake_parent_turn", fake_wake)
    r = await worker_bus.assign_to_worker("SENDER", "W1", "收尾并汇报")
    assert r["receipt"] == "absorbed" and seen["origin"] == "worker"
    seen["outcome"] = "steered"
    r = await worker_bus.assign_to_worker("SENDER", "W1", "再试")
    assert r["receipt"] == "queued"
    seen["outcome"] = "refused:nonlive"
    r = await worker_bus.assign_to_worker("SENDER", "W1", "再试")
    assert r["receipt"] == "refused" and r["reason"] == "refused:nonlive"


async def test_assign_refuses_terminal_and_empty_goal():
    _put_meta(SLUG, _worker_meta("WD", status="done"))
    r = await worker_bus.assign_to_worker("SENDER", "WD", "task")
    assert r["receipt"] == "refused" and r["reason"] == "terminal"
    r = await worker_bus.assign_to_worker("SENDER", "WD", "  ")
    assert r["receipt"] == "refused" and r["reason"] == "goal-empty"


# ----------------------------- watch 注册 ----------------------------------- #


async def test_watch_registers_and_shortcircuits_terminal():
    _put_meta(SLUG, _worker_meta("W1", status="running"))
    r = await worker_bus.watch_worker("SUB", "W1")
    # meta running + 进程内无活跃信号 → unknown（设计语义：重启后未唤醒）
    assert r["ok"] and r["watched"] and r["status"] == "unknown"
    meta = next(m for m in _session_meta_list(SLUG) if m["id"] == "W1")
    (sub,) = meta["subscribers"]
    assert sub["session_id"] == "SUB" and sub["want"] == "terminal"
    assert "W1" in ww._WATCHED
    # 终态目标：短路返回摘要，不注册
    _put_meta(SLUG, _worker_meta("WD", status="done"))
    r = await worker_bus.watch_worker("SUB", "WD")
    assert r["already_settled"] and r["status"] == "done"
    assert r["result_summary"].startswith("结论")
    meta = next(m for m in _session_meta_list(SLUG) if m["id"] == "WD")
    assert not meta.get("subscribers")


async def test_watch_idempotent_and_cap():
    _put_meta(SLUG, _worker_meta("W1", status="running"))
    await worker_bus.watch_worker("SUB", "W1")
    await worker_bus.watch_worker("SUB", "W1")  # 刷新不重复
    for i in range(worker_bus.SUBSCRIBER_CAP - 1):
        await worker_bus.watch_worker(f"S{i}", "W1")
    meta = next(m for m in _session_meta_list(SLUG) if m["id"] == "W1")
    assert len(meta["subscribers"]) == worker_bus.SUBSCRIBER_CAP
    r = await worker_bus.watch_worker("OVER", "W1")
    assert not r["ok"] and "cap" in r["error"]


# ----------------------------- notify_settled ------------------------------- #


@pytest.fixture
def inject_calls(monkeypatch):
    calls: list = []

    async def fake_inject(sub_id, text, child_id, extra_kwargs=None):
        calls.append({"sub": sub_id, "text": text, "child": child_id,
                      "extra": extra_kwargs or {}})

    monkeypatch.setattr(ww, "_inject_result", fake_inject)
    return calls


async def test_notify_settled_fires_once_and_skips_ancestors(inject_calls):
    _put_meta(SLUG, _worker_meta("W1", status="done", parent="P1"))
    _put_meta(SLUG, _subagent_parent("P1", parent="GP1"))
    _put_meta(SLUG, _subagent_parent("GP1"))
    _put_meta(SLUG, _main_meta("SUB"))
    subs = [
        {"session_id": "P1", "want": "terminal", "since": 1},   # 父：血缘已覆盖
        {"session_id": "GP1", "want": "terminal", "since": 1},  # 祖父：同上
        {"session_id": "SUB", "want": "terminal", "since": 1},  # 树外订阅者 → 触发
        {"session_id": "DG", "want": "digest", "since": 1},     # digest 不消耗
    ]
    from ginno_runtime.session_meta import _session_meta_patch

    _session_meta_patch(SLUG, "W1", {"subscribers": subs})
    ww._WATCHED.add("W1")
    await worker_bus.notify_settled("W1")
    assert len(inject_calls) == 1 and inject_calls[0]["sub"] == "SUB"
    assert "<ginno_worker_result" in inject_calls[0]["text"]
    assert 'status="done"' in inject_calls[0]["text"]
    assert inject_calls[0]["child"] == "W1"
    assert inject_calls[0]["extra"]["ginno_worker_msg"]["from"] == "W1"
    meta = next(m for m in _session_meta_list(SLUG) if m["id"] == "W1")
    # 终态订阅（含血缘）被移除，digest 保留
    assert [s["session_id"] for s in meta["subscribers"]] == ["DG"]
    # 幂等：再来一次无订阅可触
    await worker_bus.notify_settled("W1")
    assert len(inject_calls) == 1


def _subagent_parent(sid: str, parent: str | None = None) -> dict:
    m = _worker_meta(sid, status="running")
    m["parent_session_id"] = parent
    m["depth"] = 0
    return m


async def test_notify_settled_failed_and_stopped(inject_calls):
    _put_meta(SLUG, _worker_meta("WF", status="failed", subscribers=[
        {"session_id": "SUB", "want": "terminal", "since": 1}]))
    _put_meta(SLUG, _worker_meta("WS", status="stopped", subscribers=[
        {"session_id": "SUB", "want": "terminal", "since": 1}]))
    await worker_bus.notify_settled("WF")
    await worker_bus.notify_settled("WS")
    assert {c["sub"] for c in inject_calls} == {"SUB"}
    statuses = [c["text"] for c in inject_calls]
    assert any('status="failed"' in t for t in statuses)
    assert any('status="stopped"' in t for t in statuses)


# ----------------------------- digest 节流 ---------------------------------- #


async def test_digest_hook_throttles(monkeypatch, inject_calls):
    _put_meta(SLUG, _worker_meta("W1", status="running", subscribers=[
        {"session_id": "SUB", "want": "digest", "since": 1, "last_digest": 0}]))
    ww._WATCHED.add("W1")

    async def fake_excerpt(slug, sid, limit=300):
        return "正在跑第三轮测试"

    monkeypatch.setattr(ww, "_last_assistant_excerpt", fake_excerpt)
    await worker_bus.on_turn_settled_hook("W1")
    assert len(inject_calls) == 1
    assert 'kind="digest"' in inject_calls[0]["text"]
    assert "正在跑第三轮测试" in inject_calls[0]["text"]
    # 节流窗口内第二次 settle：不推
    await worker_bus.on_turn_settled_hook("W1")
    assert len(inject_calls) == 1
    # 未订阅的会话：零成本直接返回（不抛、不投）
    await worker_bus.on_turn_settled_hook("NOT-WATCHED")
    assert len(inject_calls) == 1


# ----------------------------- 启动对账 ------------------------------------- #


async def test_reconcile_fires_missed_notifications(monkeypatch, inject_calls):
    # 崩溃错过回传：终态 + 订阅仍在 → 启动补发
    _put_meta(SLUG, _worker_meta("W1", status="done", subscribers=[
        {"session_id": "SUB", "want": "terminal", "since": 1}]))
    _put_meta(SLUG, _worker_meta("W2", status="running", subscribers=[
        {"session_id": "SUB", "want": "terminal", "since": 1}]))
    worker_bus.reconcile_subscriptions()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(inject_calls) == 1 and inject_calls[0]["child"] == "W1"
    # W1 已补发（订阅清空 → 出快查表）；W2 未终态留在表里等它的 settle
    assert "W1" not in ww._WATCHED and "W2" in ww._WATCHED
    meta = next(m for m in _session_meta_list(SLUG) if m["id"] == "W1")
    assert not meta.get("subscribers")  # 已补发的移除


# ----------------------------- steer 保留 ----------------------------------- #


def test_steer_keep_channel_retains_worker_origin():
    server_shared.steer_enqueue("T1", {"steer_id": "w-1", "text": "task",
                                       "origin": "worker", "injected_at": 1})
    server_shared.steer_enqueue("T1", {"steer_id": "u-1", "text": "user msg",
                                       "injected_at": 1})
    server_shared.steer_keep_channel("T1")
    ids = [e["steer_id"] for e in server_shared._STEER_STASH["T1"]]
    assert ids == ["w-1"]  # worker 保留，无 origin 的被清


# ----------------------------- 工具面 --------------------------------------- #


def test_build_worker_tools_presence():
    assert build_worker_tools(None) == []
    names = [t.name for t in build_worker_tools("S1", SLUG)]
    assert names == ["list_workers", "assign_worker", "watch_worker", "worker_status"]


async def test_wake_outcome_strings_from_real_function():
    """出口码契约：真 _wake_parent_turn 对 terminal 目标返回 refused:terminal。"""
    _put_meta(SLUG, _worker_meta("WT", status="done"))
    out = await sched._wake_parent_turn("WT", "x", None)
    assert out == "refused:terminal"


async def test_inject_result_extra_kwargs_rides_steer():
    _put_meta(SLUG, _main_meta("P1"))
    server_shared._RUNNING_TURNS["P1"] = "turn-1"
    await sched._inject_result(
        "P1", "text", "C1", extra_kwargs={"ginno_worker_msg": {"from": "C1"}}
    )
    (entry,) = server_shared._STEER_STASH["P1"]
    assert entry["extra_kwargs"] == {
        "ginno_subagent_result": "C1",
        "ginno_worker_msg": {"from": "C1"},
    }
