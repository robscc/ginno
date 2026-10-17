"""Unit tests for the scheduler (scheduled-tasks-design.md §3.7).

验收 §14：next_run 四种计划（含 DST 模拟）、missed 对账、双目标触发分流（fake 缝隙）、
间隔下限与孤儿 running 对账。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ginno_runtime import schedule_store as store
from ginno_runtime import scheduler

pytestmark = pytest.mark.unit

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _clean_scheduler_state():
    scheduler._RUNNING.clear()
    scheduler._RUN_TASKS.clear()
    store.reset_cache()
    yield
    scheduler._RUNNING.clear()
    scheduler._RUN_TASKS.clear()
    store.reset_cache()


def _task(**kw):
    t = {
        "id": "st-t1",
        "name": "T",
        "target": {"type": "prompt", "prompt": "p", "agent_id": "dev", "project_slug": "default"},
        "schedule": {"kind": "daily", "at": "09:30"},
        "enabled": True,
        "created": time.time(),
        "updated": time.time(),
    }
    t.update(kw)
    return t


def _wall(ts: float, tz=NY) -> tuple:
    d = datetime.fromtimestamp(ts, tz)
    return (d.year, d.month, d.day, d.hour, d.minute)


# ---- next_run：四种计划 ------------------------------------------------------
def test_next_run_daily(isolated_home):
    now = datetime(2026, 10, 2, 8, 0, tzinfo=NY).timestamp()
    nxt = scheduler.next_run_ts({"kind": "daily", "at": "09:30"}, now=now, tz=NY)
    assert _wall(nxt) == (2026, 10, 2, 9, 30)
    # 已过当日时刻 → 明天
    now2 = datetime(2026, 10, 2, 10, 0, tzinfo=NY).timestamp()
    nxt2 = scheduler.next_run_ts({"kind": "daily", "at": "09:30"}, now=now2, tz=NY)
    assert _wall(nxt2) == (2026, 10, 3, 9, 30)


def test_next_run_daily_dst_spring_forward(isolated_home):
    """每日 09:30 在 DST 切换日（2026-03-08，纽约 02:00→03:00）仍是本地 09:30。"""
    now = datetime(2026, 3, 7, 12, 0, tzinfo=NY).timestamp()
    nxt = scheduler.next_run_ts({"kind": "daily", "at": "09:30"}, now=now, tz=NY)
    assert _wall(nxt) == (2026, 3, 8, 9, 30)
    # 前一天语义不变（该日墙钟 23 小时，日历语义而非固定 24h 间隔）
    prev = scheduler.next_run_ts({"kind": "daily", "at": "09:30"},
                                 now=datetime(2026, 3, 6, 12, 0, tzinfo=NY).timestamp(), tz=NY)
    assert _wall(prev) == (2026, 3, 7, 9, 30)


def test_next_run_daily_nonexistent_time_skips_forward(isolated_home):
    """DST 春季前拨日 02:30 不存在 → 顺延到存在的本地时间（03:30）。"""
    now = datetime(2026, 3, 7, 12, 0, tzinfo=NY).timestamp()
    nxt = scheduler.next_run_ts({"kind": "daily", "at": "02:30"}, now=now, tz=NY)
    d = datetime.fromtimestamp(nxt, NY)
    assert (d.year, d.month, d.day) == (2026, 3, 8)
    assert d.hour == 3 and d.minute == 30


def test_next_run_weekly_sunday_is_zero(isolated_home):
    """契约：weekday 0=周日（§4.1）。2026-10-02 是周五。"""
    now = datetime(2026, 10, 2, 8, 0, tzinfo=NY).timestamp()  # Friday
    # 周日（0）18:00 → 2026-10-04
    nxt = scheduler.next_run_ts({"kind": "weekly", "weekday": 0, "at": "18:00"}, now=now, tz=NY)
    assert _wall(nxt) == (2026, 10, 4, 18, 0)
    # 周五（5）18:00 → 当天
    nxt2 = scheduler.next_run_ts({"kind": "weekly", "weekday": 5, "at": "18:00"}, now=now, tz=NY)
    assert _wall(nxt2) == (2026, 10, 2, 18, 0)
    # 周五 18:00 已过 → 下周五
    now2 = datetime(2026, 10, 2, 19, 0, tzinfo=NY).timestamp()
    nxt3 = scheduler.next_run_ts({"kind": "weekly", "weekday": 5, "at": "18:00"}, now=now2, tz=NY)
    assert _wall(nxt3) == (2026, 10, 9, 18, 0)


def test_next_run_weekly_multi_day(isolated_home):
    """weekdays 多选（0=周日）。2026-10-02 是周五：早 8 点选 [周日,周五]
    → 当天 18:00；晚 19 点 → 跳到周日；与旧单值 weekday 语义等价。"""
    now = datetime(2026, 10, 2, 8, 0, tzinfo=NY).timestamp()
    nxt = scheduler.next_run_ts({"kind": "weekly", "weekdays": [0, 5], "at": "18:00"}, now=now, tz=NY)
    assert _wall(nxt) == (2026, 10, 2, 18, 0)
    now2 = datetime(2026, 10, 2, 19, 0, tzinfo=NY).timestamp()
    nxt2 = scheduler.next_run_ts({"kind": "weekly", "weekdays": [0, 5], "at": "18:00"}, now=now2, tz=NY)
    assert _wall(nxt2) == (2026, 10, 4, 18, 0)
    assert nxt2 == scheduler.next_run_ts({"kind": "weekly", "weekday": 0, "at": "18:00"}, now=now2, tz=NY)
    # 工作日组合：周五 19 点选 [周一,周三] → 下周一
    nxt3 = scheduler.next_run_ts({"kind": "weekly", "weekdays": [1, 3], "at": "18:00"}, now=now2, tz=NY)
    assert _wall(nxt3) == (2026, 10, 5, 18, 0)


def test_step_point_weekly_multi(isolated_home):
    """多选 weekly 的 missed 链按所选天步进：周五点 → 下一个所选天（周一）。"""
    wk = _task(schedule={"kind": "weekly", "weekdays": [1, 4], "at": "18:00"})  # 周一、周四
    pt = datetime(2026, 10, 2, 18, 0).astimezone().timestamp()  # 周五
    d1 = datetime.fromtimestamp(scheduler._step_point(wk, time.time(), pt))
    assert (d1.year, d1.month, d1.day, d1.hour, d1.minute) == (2026, 10, 5, 18, 0)
    d2 = datetime.fromtimestamp(scheduler._step_point(wk, time.time(), d1.timestamp()))
    assert (d2.year, d2.month, d2.day, d2.hour, d2.minute) == (2026, 10, 8, 18, 0)


def test_planned_points_weekly_multi(isolated_home):
    """时间条计划点：只在所选天出点，其他天为空。"""
    task = _task(schedule={"kind": "weekly", "weekdays": [0, 5], "at": "18:00"})
    now = datetime(2026, 10, 2, 8, 0).astimezone().timestamp()  # 周五
    pts = scheduler.planned_points(task, "2026-10-02", now=now)
    assert [datetime.fromtimestamp(p).strftime("%Y-%m-%d %H:%M") for p in pts] == ["2026-10-02 18:00"]
    assert scheduler.planned_points(task, "2026-10-03", now=now) == []


def test_next_run_once(isolated_home):
    now = datetime(2026, 10, 2, 8, 0, tzinfo=NY).timestamp()
    nxt = scheduler.next_run_ts({"kind": "once", "at": "2026-10-05T18:00:00"}, now=now, tz=NY)
    assert _wall(nxt) == (2026, 10, 5, 18, 0)
    past = scheduler.next_run_ts({"kind": "once", "at": "2026-10-01T18:00:00"}, now=now, tz=NY)
    assert past is None


def test_interval_grid_anchor(isolated_home):
    sch = {"kind": "interval", "minutes": 5}  # 300s 网格
    now = 1000.0
    # 从上一计划点锚定网格（不做 now 之上的跨步；追赶由 missed 链逐点记账）
    assert scheduler.compute_next(_task(schedule=sch), now, from_point=900.0) == 1200.0
    # 直接计算的下一个点 = now + 间隔
    assert scheduler.compute_next(_task(schedule=sch), now) == now + 300


# ---- missed 对账 ------------------------------------------------------------
def test_step_point_daily_steps_from_point(isolated_home):
    """日历类的 missed 链从上一个计划点步进（而非从 now 重算）：多日停机时
    每个错过的计划点各记一条 missed（§3.7），而不是只记一条。
    compute_next 走系统本地时区（与 _tick 一致），这里用本地墙上时间构造。"""
    task = _task()
    pt = datetime(2026, 10, 1, 9, 30).astimezone().timestamp()
    d = datetime.fromtimestamp(scheduler._step_point(task, time.time(), pt))
    assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 10, 2, 9, 30)
    # 步进链严格递增，跨周的 weekly 同理（2026-09-25 与 10-02 都是周五）
    wk = _task(schedule={"kind": "weekly", "weekday": 5, "at": "18:00"})
    pt2 = datetime(2026, 9, 25, 18, 0).astimezone().timestamp()
    d1 = datetime.fromtimestamp(scheduler._step_point(wk, time.time(), pt2))
    assert (d1.year, d1.month, d1.day, d1.hour, d1.minute) == (2026, 10, 2, 18, 0)
    d2 = datetime.fromtimestamp(scheduler._step_point(wk, time.time(), d1.timestamp()))
    assert (d2.year, d2.month, d2.day, d2.hour, d2.minute) == (2026, 10, 9, 18, 0)


async def test_tick_records_missed_and_skips_to_future(isolated_home, monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "trigger_task", lambda task, **k: fired.append(task["id"]))
    now = time.time()
    store.save_config({"enabled": True, "keep_awake": False, "tasks": [
        _task(schedule={"kind": "interval", "minutes": 30}, next_run_at=now - 3600),
    ]})
    await scheduler._tick()
    day_rows = store.load_day(time.strftime("%Y-%m-%d", time.localtime(now)))
    missed = [r for r in day_rows if r["status"] == "missed"]
    # 1 小时前的 30 分钟网格：错过 2 个计划点（-60min、-30min）各记一条；
    # 当前时点（now，延迟 0s ≤ GRACE）保留为下一个触发点
    assert len(missed) == 2
    assert all(r["trigger"] == "schedule" for r in missed)
    assert fired == []  # 只记账不补跑（决议 1）
    task = store.get_task("st-t1")
    assert abs(task["next_run_at"] - now) < 2


async def test_tick_triggers_within_grace(isolated_home, monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "trigger_task", lambda task, **k: fired.append((task["id"], k)))
    now = time.time()
    store.save_config({"enabled": True, "keep_awake": False, "tasks": [
        _task(next_run_at=now - 10),  # 延迟 10s ≤ GRACE
    ]})
    await scheduler._tick()
    assert len(fired) == 1 and fired[0][0] == "st-t1"
    assert fired[0][1]["trigger"] == "schedule"
    assert abs(fired[0][1]["scheduled_at"] - (now - 10)) < 1


async def test_tick_disabled_and_global_off(isolated_home, monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler, "trigger_task", lambda task, **k: fired.append(task["id"]))
    now = time.time()
    store.save_config({"enabled": False, "keep_awake": False, "tasks": [
        _task(next_run_at=now - 10),
    ]})
    await scheduler._tick()
    assert fired == []
    store.save_config({"enabled": True, "keep_awake": False, "tasks": [
        _task(next_run_at=now - 10, enabled=False),
    ]})
    await scheduler._tick()
    assert fired == []


async def test_missed_once_task_disables(isolated_home, monkeypatch):
    monkeypatch.setattr(scheduler, "trigger_task", lambda task, **k: None)
    now = time.time()
    yesterday = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 86400))
    store.save_config({"enabled": True, "keep_awake": False, "tasks": [
        _task(schedule={"kind": "once", "at": yesterday}, next_run_at=now - 86400),
    ]})
    await scheduler._tick()
    task = store.get_task("st-t1")
    assert task["enabled"] is False
    assert not task["next_run_at"]


# ---- 启动对账：孤儿 running → error("interrupted") ----------------------------
def test_reconcile_interrupted(isolated_home):
    store.record_run({"run_id": "sr-a", "task_id": "st-1", "task_name": "T",
                      "status": "running", "scheduled_at": time.time(), "started_at": time.time()})
    store.record_run({"run_id": "sr-b", "task_id": "st-1", "task_name": "T",
                      "status": "ok", "scheduled_at": time.time(), "started_at": time.time(),
                      "finished_at": time.time()})
    healed = scheduler.reconcile_interrupted()
    assert healed == 1
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    assert rows["sr-a"]["status"] == "error"
    assert rows["sr-a"]["error"] == "interrupted"
    assert rows["sr-b"]["status"] == "ok"  # 终态行不动


def test_reconcile_interrupted_yesterday_file(isolated_home):
    """跨午夜存活（昨天启动、之后被杀）的孤儿 running 行也要对账（§3.7）。"""
    ts = time.time() - 86400
    store.record_run({"run_id": "sr-old", "task_id": "st-1", "task_name": "T",
                      "status": "running", "scheduled_at": ts, "started_at": ts})
    assert scheduler.reconcile_interrupted() == 1
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(day))}
    assert rows["sr-old"]["status"] == "error"
    assert rows["sr-old"]["error"] == "interrupted"


# ---- 执行取消：重叠槽释放 + interrupted 终态 -----------------------------------
async def test_cancelled_run_releases_slot_and_records_interrupted(isolated_home, monkeypatch):
    """停机取消在途执行：_RUNNING 槽必须释放（否则后续触发永远 skipped_overlap），
    并落 error("interrupted") 终态行（与启动对账同语义）。"""
    from ginno_runtime import schedule_store as store

    async def hang(task, row):
        await asyncio.sleep(3600)

    monkeypatch.setattr(scheduler, "_run_prompt_target", hang)
    task = _task()
    rid = scheduler.trigger_task(task, trigger="manual")
    t = scheduler._RUN_TASKS[rid]
    assert scheduler._RUNNING.get(task["id"]) == rid
    await asyncio.sleep(0.01)  # 让执行协程跑到挂起点（真实停机场景：在途执行）
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert scheduler._RUNNING.get(task["id"]) is None
    rows = store.dedupe_runs(store.load_day(store._today()))
    assert rows[0]["run_id"] == rid
    assert rows[0]["status"] == "error" and rows[0]["error"] == "interrupted"


# ---- 同任务不重叠 ------------------------------------------------------------
async def test_overlap_records_skipped(isolated_home, monkeypatch):
    executed = []

    async def fake_execute(task, row):
        executed.append(row["run_id"])

    monkeypatch.setattr(scheduler, "_execute_run", fake_execute)
    task = _task()
    scheduler._RUNNING[task["id"]] = "sr-live"
    rid = scheduler.trigger_task(task, trigger="manual")
    rows = store.dedupe_runs(store.load_day(store._today()))
    assert rows[0]["status"] == "skipped_overlap"
    assert rows[0]["run_id"] == rid
    assert executed == []  # 没有新执行


# ---- 双目标分流（fake 缝隙）---------------------------------------------------
async def test_prompt_target_dispatch(isolated_home, monkeypatch):
    """prompt 目标：走影子会话 + headless _run_stream，usage_source=schedule。"""
    from ginno_runtime import server_shared as shared
    from ginno_runtime.session_meta import _session_meta_list

    captured = {}
    sid = "shadow123"

    async def fake_create_session(req):
        assert req.type == "scheduled"
        shared._SESSIONS[sid] = {
            "session_id": sid, "project_slug": req.project_slug,
            "graph": object(), "agent_id": "dev",
        }
        from ginno_runtime.session_meta import _session_meta_upsert

        _session_meta_upsert(req.project_slug, {"id": sid, "type": req.type, "title": req.title})
        return {"id": sid, "ok": True, "type": req.type}

    async def fake_run_stream(ws, graph, config, text, session, agent_id):
        captured["config"] = config
        captured["text"] = text

    monkeypatch.setattr("ginno_runtime.api.sessions.create_session", fake_create_session)
    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_run_stream)
    monkeypatch.setattr("ginno_runtime.api.sessions._turn_last_error", lambda s, t: None)

    async def fake_last_text(session_id, slug):
        return "巡检完成：无新增错误。"

    monkeypatch.setattr(scheduler, "_last_assistant_text", fake_last_text)

    task = _task()
    rid = scheduler.trigger_task(task, trigger="manual")
    await scheduler._RUN_TASKS[rid]

    # 影子会话 meta：type=scheduled + schedule_run_id（§4.3）
    metas = _session_meta_list("default")
    meta = next(m for m in metas if m["id"] == sid)
    assert meta["type"] == "scheduled"
    assert meta["schedule_run_id"] == rid
    # headless 协议：usage_source=schedule、user_text=任务提示词
    assert captured["config"]["configurable"]["usage_source"] == "schedule"
    assert captured["text"] == "p"
    # 执行记录：running → ok 两行，末行胜出带 summary
    rows = store.dedupe_runs(store.load_day(store._today()))
    assert len(rows) == 1 and rows[0]["status"] == "ok"
    assert rows[0]["run_id"] == rid
    assert rows[0]["session_id"] == sid
    assert rows[0]["summary"] and "巡检" in rows[0]["summary"]
    assert scheduler._RUNNING.get(task["id"]) is None  # 释放重叠槽


async def test_prompt_target_model_passthrough(isolated_home, monkeypatch):
    """任务级模型覆盖：CreateSessionRequest 带上 provider/model（走既有解析链，
    provider 被禁用时 _resolve_provider_model 兜底）；未配置则不传。"""
    from ginno_runtime import server_shared as shared

    captured = {}
    sid = "shadow789"

    async def fake_create_session(req):
        captured["req"] = req
        shared._SESSIONS[sid] = {
            "session_id": sid, "project_slug": req.project_slug,
            "graph": object(), "agent_id": "dev",
        }
        from ginno_runtime.session_meta import _session_meta_upsert

        _session_meta_upsert(req.project_slug, {"id": sid, "type": req.type, "title": req.title})
        return {"id": sid, "ok": True, "type": req.type}

    async def fake_run_stream(ws, graph, config, text, session, agent_id):
        pass

    async def fake_last_text(session_id, slug):
        return ""  # 空 text → 不触发 LLM 摘要后台任务

    monkeypatch.setattr("ginno_runtime.api.sessions.create_session", fake_create_session)
    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_run_stream)
    monkeypatch.setattr("ginno_runtime.api.sessions._turn_last_error", lambda s, t: None)
    monkeypatch.setattr(scheduler, "_last_assistant_text", fake_last_text)

    rid = scheduler.trigger_task(_task(model={"provider": "prov-a", "model": "big-model"}), trigger="manual")
    await scheduler._RUN_TASKS[rid]
    assert captured["req"].provider == "prov-a"
    assert captured["req"].model == "big-model"

    captured.clear()
    rid2 = scheduler.trigger_task(_task(), trigger="manual")
    await scheduler._RUN_TASKS[rid2]
    assert captured["req"].provider is None and captured["req"].model is None


async def test_prompt_running_row_has_session_id(isolated_home, monkeypatch):
    """运行中即可回放：影子会话创建后 session_id 立即回填 running 行（末行胜出）。"""
    from ginno_runtime import server_shared as shared
    from ginno_runtime.session_meta import _session_meta_upsert

    sid = "shadow456"
    seen = {}

    async def fake_create_session(req):
        shared._SESSIONS[sid] = {
            "session_id": sid, "project_slug": req.project_slug,
            "graph": object(), "agent_id": "dev",
        }
        _session_meta_upsert(req.project_slug, {"id": sid, "type": req.type, "title": req.title})
        return {"id": sid, "ok": True, "type": req.type}

    async def fake_run_stream(ws, graph, config, text, session, agent_id):
        # 执行仍在跑：此刻落盘的末行应已带 session_id 且 status=running
        rows = store.dedupe_runs(store.load_day(store._today()))
        seen["row"] = next(r for r in rows if r.get("session_id") == sid)

    monkeypatch.setattr("ginno_runtime.api.sessions.create_session", fake_create_session)
    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_run_stream)
    monkeypatch.setattr("ginno_runtime.api.sessions._turn_last_error", lambda s, t: None)

    async def fake_last_text(session_id, slug):
        return "done"

    monkeypatch.setattr(scheduler, "_last_assistant_text", fake_last_text)
    rid = scheduler.trigger_task(_task(), trigger="manual")
    await scheduler._RUN_TASKS[rid]
    assert seen["row"]["status"] == "running"
    assert seen["row"]["session_id"] == sid


async def test_workflow_missing_records_error(isolated_home):
    task = _task(target={"type": "workflow", "workflow_id": "gone-wf"})
    rid = scheduler.trigger_task(task, trigger="manual")
    await scheduler._RUN_TASKS[rid]
    rows = store.dedupe_runs(store.load_day(store._today()))
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "workflow_missing"


async def test_workflow_missing_input_records_error(isolated_home):
    from ginno_runtime.workflows import store as wf_store

    wf = wf_store.create_def({
        "name": "NeedsInput",
        "dsl": {
            "entry": "s1",
            "context": {
                "schema": {
                    "type": "object",
                    "properties": {"repo": {"type": "string"}},
                    "required": ["repo"],
                },
                "initial": {"repo": ""},
            },
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "g"}],
            "edges": [],
        },
    })
    task = _task(target={"type": "workflow", "workflow_id": wf["id"]})
    rid = scheduler.trigger_task(task, trigger="manual")
    await scheduler._RUN_TASKS[rid]
    rows = store.dedupe_runs(store.load_day(store._today()))
    assert rows[0]["status"] == "error"
    assert rows[0]["error"].startswith("missing_input")
    assert "repo" in rows[0]["error"]


# ---- 触发后 next_run_at 必须越过已触发点（三个 run 根因回归钉）----------------
async def test_trigger_advances_past_fired_point(isolated_home, monkeypatch):
    """2026-10-07/08/09「同一计划点出 3 条 run」的根因：_advance_next 曾把
    next_run_at 原样写回刚触发的点（GRACE 内 _skip_missed 不跳它），随后
    tick 再触发 → skipped_overlap / 重复真跑，GRACE 过后还补一条幽灵 missed。
    修后：触发即推进到严格未来；后续 tick（含 GRACE 内外）零产出。"""

    async def fake_execute(task, row):
        await asyncio.sleep(3600)  # 模拟长跑（> GRACE），停在 running

    monkeypatch.setattr(scheduler, "_execute_run", fake_execute)
    now = time.time()
    store.save_config({"enabled": True, "keep_awake": False, "tasks": [
        _task(next_run_at=now - 2),  # 刚到点（延迟 2s ≤ GRACE）
    ]})
    await scheduler._tick()
    rid = next(iter(scheduler._RUNNING.values()))
    assert rid  # 触发了

    # next_run_at 必须严格在未来（daily → 明天同一时刻），不是刚触发的点
    t = store.get_task("st-t1")
    assert t["next_run_at"] > now + 3600

    # 后续 tick（模拟 +30s 的 skipped_overlap 时点与 +61s/+120s 的 missed
    # 时点）：不再触发、不再记账。fake clock 只替换 scheduler 模块内的
    # time 引用，不动全局。
    from types import SimpleNamespace

    real_time = scheduler.time
    for delta in (30, 61, 120):
        monkeypatch.setattr(
            scheduler, "time", SimpleNamespace(time=lambda d=delta: now - 2 + d)
        )
        await scheduler._tick()
    monkeypatch.setattr(scheduler, "time", real_time)
    for t in list(scheduler._RUN_TASKS.values()):
        t.cancel()
    rows = store.dedupe_runs(store.load_day(store._today()))
    assert len(rows) == 1
    assert rows[0]["run_id"] == rid and rows[0]["status"] == "running"
    # 任务也不会被 once 语义误停用
    assert store.get_task("st-t1")["enabled"] is True


async def test_tick_recomputes_invalid_weekly_next_run(isolated_home, monkeypatch):
    """weekly next_run_at 落在非计划星期(脏数据,2026-10-17 实测:旧二进制把
    Mon-Fri 任务的 next_run_at 写到周日,周五整点被静默跳过且无 missed 记账)
    → tick 丢弃该落点、从 now 重算,不触发也不记 missed。"""
    fired = []
    monkeypatch.setattr(scheduler, "trigger_task", lambda task, **k: fired.append(task["id"]))
    now = time.time()
    # 找一个“now 之后最近的周日 06:00”——周日不在 Mon-Fri 集合内
    sunday = now + 86400
    while datetime.fromtimestamp(sunday).weekday() != 6 or datetime.fromtimestamp(sunday).hour < 7:
        sunday += 86400
    d = datetime.fromtimestamp(sunday).replace(hour=6, minute=0, second=0, microsecond=0)
    sunday = d.timestamp()
    store.save_config({"enabled": True, "keep_awake": False, "tasks": [
        _task(schedule={"kind": "weekly", "weekdays": [1, 2, 3, 4, 5], "at": "06:00"},
              next_run_at=sunday),
    ]})
    await scheduler._tick()
    assert fired == []
    day_rows = store.load_day(time.strftime("%Y-%m-%d", time.localtime(now)))
    assert [r for r in day_rows if r["status"] == "missed"] == []
    task = store.get_task("st-t1")
    nxt = task["next_run_at"]
    assert nxt > now
    assert datetime.fromtimestamp(nxt).weekday() in {0, 1, 2, 3, 4}  # 落回 Mon-Fri


# ---- 启动对账自动续跑（2026-10-17）--------------------------------------------
def _save_task(**kw):
    t = _task(**kw)
    store.upsert_task(t)
    return t


async def test_reconcile_resumes_recent_prompt_run(isolated_home, monkeypatch):
    """窗口内的孤儿 running 行（有 session_id、任务仍启用）→ 同 run_id 落回
    running、重占重叠槽、spawn 续跑协程，终态/last_run 正常回写。"""
    _save_task()
    row = {"run_id": "sr-r", "task_id": "st-t1", "task_name": "T", "target_type": "prompt",
           "status": "running", "scheduled_at": time.time(), "started_at": time.time(),
           "finished_at": None, "session_id": "sess-1", "summary": None, "error": None}
    store.record_run(row)
    calls = []

    async def fake_resume(task, r):
        calls.append((task["id"], r["run_id"], r["session_id"]))
        return {"ok": True, "error": None, "text": "resumed!", "input_tokens": 1, "output_tokens": 2}

    monkeypatch.setattr(scheduler, "_resume_prompt_target", fake_resume)
    healed = scheduler.reconcile_interrupted()
    assert healed == 0
    assert scheduler._RUNNING.get("st-t1") == "sr-r"
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    assert rows["sr-r"]["status"] == "running"  # 续跑行（末行胜出）
    assert calls == []
    await scheduler._RUN_TASKS["sr-r"]  # 续跑协程收尾
    assert calls == [("st-t1", "sr-r", "sess-1")]
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    assert rows["sr-r"]["status"] == "ok"
    assert rows["sr-r"]["summary"] == "resumed!"
    task = store.get_task("st-t1")
    assert task["last_run"]["status"] == "ok"
    assert scheduler._RUNNING.get("st-t1") is None  # 槽已释放


async def test_reconcile_resume_workflow_target(isolated_home, monkeypatch):
    """workflow 目标续跑分流：走 _resume_workflow_target（以行内 workflow_run_id 为锚）。"""
    _save_task(target={"type": "workflow", "workflow_id": "wf-1"})
    row = {"run_id": "sr-w", "task_id": "st-t1", "task_name": "T", "target_type": "workflow",
           "status": "running", "scheduled_at": time.time(), "started_at": time.time(),
           "finished_at": None, "workflow_id": "wf-1", "workflow_run_id": "wfr-1",
           "summary": None, "error": None}
    store.record_run(row)
    calls = []

    async def fake_resume(task, r):
        calls.append(r["workflow_run_id"])
        return {"ok": True, "error": None, "text": "wf resumed", "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(scheduler, "_resume_workflow_target", fake_resume)
    scheduler.reconcile_interrupted()
    await scheduler._RUN_TASKS["sr-w"]
    assert calls == ["wfr-1"]
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    assert rows["sr-w"]["status"] == "ok"


def test_reconcile_stale_run_marks_interrupted(isolated_home):
    """超窗（>24h）孤儿行不续跑，照旧记 interrupted。"""
    _save_task()
    ts = time.time() - scheduler.RESUME_WINDOW_S - 60
    store.record_run({"run_id": "sr-old", "task_id": "st-t1", "task_name": "T",
                      "target_type": "prompt", "status": "running", "scheduled_at": ts,
                      "started_at": ts, "finished_at": None, "session_id": "sess-1",
                      "summary": None, "error": None})
    assert scheduler.reconcile_interrupted() == 1
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(day))}
    assert rows["sr-old"]["status"] == "error"
    assert rows["sr-old"]["error"] == "interrupted"
    assert scheduler._RUNNING.get("st-t1") is None


def test_reconcile_task_deleted_or_disabled_marks_interrupted(isolated_home):
    """任务已删除 / 已停用 → 不续跑，记 interrupted。"""
    _save_task(enabled=False)
    ts = time.time()
    store.record_run({"run_id": "sr-d1", "task_id": "st-t1", "task_name": "T",
                      "target_type": "prompt", "status": "running", "scheduled_at": ts,
                      "started_at": ts, "finished_at": None, "session_id": "sess-1",
                      "summary": None, "error": None})
    # 任务已删除：行指向不存在的 st-gone
    store.record_run({"run_id": "sr-d2", "task_id": "st-gone", "task_name": "G",
                      "target_type": "prompt", "status": "running", "scheduled_at": ts,
                      "started_at": ts, "finished_at": None, "session_id": "sess-2",
                      "summary": None, "error": None})
    assert scheduler.reconcile_interrupted() == 2
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    for rid in ("sr-d1", "sr-d2"):
        assert rows[rid]["status"] == "error" and rows[rid]["error"] == "interrupted"


def test_reconcile_no_anchor_marks_interrupted(isolated_home):
    """无续跑锚点（会话还没建就被杀，session_id 为空）→ interrupted。"""
    _save_task()
    ts = time.time()
    store.record_run({"run_id": "sr-na", "task_id": "st-t1", "task_name": "T",
                      "target_type": "prompt", "status": "running", "scheduled_at": ts,
                      "started_at": ts, "finished_at": None, "session_id": None,
                      "summary": None, "error": None})
    assert scheduler.reconcile_interrupted() == 1


def test_reconcile_resume_without_loop_falls_back_to_interrupted(isolated_home):
    """无事件循环（同步直调）时不静默悬挂：可续跑行退回 interrupted 记账。"""
    _save_task()
    ts = time.time()
    store.record_run({"run_id": "sr-nl", "task_id": "st-t1", "task_name": "T",
                      "target_type": "prompt", "status": "running", "scheduled_at": ts,
                      "started_at": ts, "finished_at": None, "session_id": "sess-1",
                      "summary": None, "error": None})
    assert scheduler.reconcile_interrupted() == 1
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    assert rows["sr-nl"]["status"] == "error"


async def test_resume_cancelled_records_interrupted(isolated_home, monkeypatch):
    """续跑被停机取消（再次打断）：落 interrupted 行并释放重叠槽。"""
    _save_task()
    ts = time.time()
    row = {"run_id": "sr-c", "task_id": "st-t1", "task_name": "T", "target_type": "prompt",
           "status": "running", "scheduled_at": ts, "started_at": ts,
           "finished_at": None, "session_id": "sess-1", "summary": None, "error": None}
    store.record_run(row)

    async def hang(task, r):
        await asyncio.sleep(3600)

    monkeypatch.setattr(scheduler, "_resume_prompt_target", hang)
    scheduler.reconcile_interrupted()
    t = scheduler._RUN_TASKS["sr-c"]
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert scheduler._RUNNING.get("st-t1") is None
    rows = {r["run_id"]: r for r in store.dedupe_runs(store.load_day(store._today()))}
    assert rows["sr-c"]["status"] == "error" and rows["sr-c"]["error"] == "interrupted"
