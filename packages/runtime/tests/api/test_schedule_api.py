"""API tests for /api/schedule endpoints (scheduled-tasks-design.md §5.1).

验收 §10（workflow_missing 之外）/§11 摘要不缺席/§13 usage source=schedule。
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import schedule_store as store
from ginno_runtime import scheduler
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def _clean_state():
    scheduler._RUNNING.clear()
    scheduler._RUN_TASKS.clear()
    store.reset_cache()
    yield
    scheduler._RUNNING.clear()
    scheduler._RUN_TASKS.clear()
    store.reset_cache()


def _prompt_task(**kw):
    body = {
        "name": "日志巡检",
        "target": {
            "type": "prompt", "prompt": "巡检日志",
            "agent_id": "dev", "project_slug": "default",
        },
        "schedule": {"kind": "daily", "at": "09:30"},
    }
    body.update(kw)
    return body


def _wf_def(store_mod, *, required=False):
    ctx = {}
    if required:
        # required 键在 initial 里留空 → 不带 context_override 保存即缺输入
        ctx = {
            "schema": {
                "type": "object", "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
            },
            "initial": {"repo": ""},
        }
    wf = store_mod.create_def({
        "name": "仓库体检",
        "dsl": {
            "entry": "s1",
            **({"context": ctx} if ctx else {}),
            "nodes": [{
                "id": "s1", "type": "step", "agent": "dev",
                "goal": "体检\nWRITE_JSON {\"ok\": true}",
            }],
            "edges": [],
        },
    })
    return wf


# ---- GET/PUT 全局开关 --------------------------------------------------------
def test_get_schedule_defaults(client):
    r = client.get("/api/schedule").json()
    assert r["enabled"] is True
    assert r["keep_awake"] is False
    assert r["tasks"] == []


def test_put_schedule_switches(client):
    r = client.put("/api/schedule", json={"enabled": False, "keep_awake": True})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["keep_awake"] is True
    assert client.get("/api/schedule").json()["enabled"] is False


# ---- 任务 CRUD + 校验 --------------------------------------------------------
def test_create_task_returns_completed_object(client):
    r = client.post("/api/schedule/tasks", json=_prompt_task())
    assert r.status_code == 200
    t = r.json()
    assert t["id"].startswith("st-")
    assert t["next_run_at"] and t["next_run_at"] > time.time()
    assert client.get("/api/schedule").json()["tasks"][0]["id"] == t["id"]


def test_create_task_interval_below_minimum_400(client):
    body = _prompt_task(schedule={"kind": "interval", "minutes": 4})
    r = client.post("/api/schedule/tasks", json=body)
    assert r.status_code == 400
    assert "5 分钟" in r.json()["detail"]


def test_create_task_weekdays_and_model(client):
    """weekly 多选 + 任务级模型覆盖一起落库；next_run_at 已算。"""
    body = _prompt_task(
        schedule={"kind": "weekly", "weekdays": [1, 3, 5], "at": "18:00"},
        model={"provider": "prov-a", "model": "m1"},
    )
    r = client.post("/api/schedule/tasks", json=body)
    assert r.status_code == 200
    t = r.json()
    assert t["schedule"]["weekdays"] == [1, 3, 5]
    assert t["model"] == {"provider": "prov-a", "model": "m1"}
    assert t["next_run_at"] > time.time()
    # 旧单值 weekday 仍可保存（读侧兼容）
    t2 = client.post("/api/schedule/tasks", json=_prompt_task(
        schedule={"kind": "weekly", "weekday": 0, "at": "09:00"}
    )).json()
    assert t2["schedule"]["weekday"] == 0


def test_create_task_bad_weekdays_400(client):
    for days in ([], [7], [True]):
        r = client.post("/api/schedule/tasks", json=_prompt_task(
            schedule={"kind": "weekly", "weekdays": days, "at": "09:00"}
        ))
        assert r.status_code == 400
    # 旧契约的坏值同样拒绝
    r2 = client.post("/api/schedule/tasks", json=_prompt_task(
        schedule={"kind": "weekly", "weekday": 7, "at": "09:00"}
    ))
    assert r2.status_code == 400


def test_patch_task_model_none_clears_and_omit_keeps(client):
    """PATCH model:null 清除覆盖；省略 model 的 PATCH 沿用现值。"""
    t = client.post("/api/schedule/tasks", json=_prompt_task(
        model={"provider": "p", "model": "m"}
    )).json()
    r = client.patch(f"/api/schedule/tasks/{t['id']}", json={"model": None})
    assert r.status_code == 200
    assert r.json().get("model") is None
    t2 = client.post("/api/schedule/tasks", json=_prompt_task(
        model={"provider": "p", "model": "m"}
    )).json()
    r2 = client.patch(f"/api/schedule/tasks/{t2['id']}", json={"name": "改名"})
    assert r2.json()["model"] == {"provider": "p", "model": "m"}


def test_patch_weekdays_recomputes_next_run(client):
    from datetime import datetime

    t = client.post("/api/schedule/tasks", json=_prompt_task(
        schedule={"kind": "weekly", "weekday": 0, "at": "09:00"}
    )).json()
    r = client.patch(f"/api/schedule/tasks/{t['id']}", json={
        "schedule": {"kind": "weekly", "weekdays": [1], "at": "09:00"}  # 改到周一
    })
    assert r.status_code == 200
    nxt = r.json()["next_run_at"]
    assert nxt and nxt > time.time()
    assert datetime.fromtimestamp(nxt).weekday() == 0  # Python Monday=0


def test_create_task_workflow_missing_required_input_400(client):
    from ginno_runtime.workflows import store as wf_store

    wf = _wf_def(wf_store, required=True)
    body = _prompt_task(target={"type": "workflow", "workflow_id": wf["id"]})  # 无 context_override
    r = client.post("/api/schedule/tasks", json=body)
    assert r.status_code == 400
    assert "repo" in r.json()["detail"]
    # 补上输入后可保存
    body["target"]["context_override"] = {"repo": "ginno"}
    assert client.post("/api/schedule/tasks", json=body).status_code == 200


def test_create_task_workflow_def_deleted_is_savable(client):
    """配方已删除的任务可保存（触发时报 workflow_missing，§3.2）。"""
    body = _prompt_task(target={"type": "workflow", "workflow_id": "ghost-wf"})
    assert client.post("/api/schedule/tasks", json=body).status_code == 200


def test_patch_task_recomputes_next_run(client):
    t = client.post("/api/schedule/tasks", json=_prompt_task()).json()
    old_next = t["next_run_at"]
    sched = {"kind": "daily", "at": "23:00"}
    r = client.patch(f"/api/schedule/tasks/{t['id']}", json={"schedule": sched})
    assert r.status_code == 200
    got = r.json()
    assert got["schedule"]["at"] == "23:00"
    assert got["next_run_at"] != old_next
    # 启停
    r2 = client.patch(f"/api/schedule/tasks/{t['id']}", json={"enabled": False})
    assert r2.json()["enabled"] is False
    assert r2.json().get("next_run_at") in (None, 0) or r2.json()["next_run_at"] is None


def test_patch_rename_keeps_next_run(client):
    """改名等与计划无关的 PATCH 不重算 next_run_at——interval 的网格锚点若被
    重置，每次改名都会把节拍平移到 now+间隔。"""
    body = _prompt_task(schedule={"kind": "interval", "minutes": 30})
    t = client.post("/api/schedule/tasks", json=body).json()
    old = t["next_run_at"]
    r = client.patch(f"/api/schedule/tasks/{t['id']}", json={"name": "新名字"})
    assert r.status_code == 200
    assert r.json()["next_run_at"] == old


def test_patch_and_delete_404(client):
    assert client.patch("/api/schedule/tasks/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/schedule/tasks/nope").status_code == 404


def test_delete_task(client):
    t = client.post("/api/schedule/tasks", json=_prompt_task()).json()
    assert client.delete(f"/api/schedule/tasks/{t['id']}").json()["ok"] is True
    assert client.get("/api/schedule").json()["tasks"] == []


# ---- 立即执行（分流）----------------------------------------------------------
def test_run_now_dispatches(client, monkeypatch):
    called = {}

    def fake_trigger(task, *, trigger="schedule", scheduled_at=None):
        called["task"] = task
        called["trigger"] = trigger
        return "sr-fake"

    monkeypatch.setattr(scheduler, "trigger_task", fake_trigger)
    t = client.post("/api/schedule/tasks", json=_prompt_task()).json()
    r = client.post(f"/api/schedule/tasks/{t['id']}/run")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "run_id": "sr-fake"}
    assert called["trigger"] == "manual"
    assert called["task"]["id"] == t["id"]
    assert client.post("/api/schedule/tasks/nope/run").status_code == 404


# ---- 执行记录 + 时间条 --------------------------------------------------------
def test_runs_endpoint_filters_and_pagination(client):
    now = time.time()
    store.record_run({
        "run_id": "sr-1", "task_id": "st-a", "task_name": "A",
        "target_type": "prompt", "trigger": "schedule", "status": "ok",
        "scheduled_at": now, "started_at": now, "finished_at": now + 10, "summary": "s",
    })
    store.record_run({
        "run_id": "sr-2", "task_id": "st-b", "task_name": "B",
        "target_type": "workflow", "trigger": "manual", "status": "error", "error": "boom",
        "scheduled_at": now, "started_at": now, "finished_at": now + 20,
    })
    r = client.get("/api/schedule/runs").json()
    assert r["total"] == 2 and r["rows"][0]["run_id"] == "sr-2"  # 默认时间降序
    asc0 = client.get("/api/schedule/runs", params={"sort": "asc"}).json()["rows"][0]
    assert asc0["run_id"] == "sr-1"
    assert client.get("/api/schedule/runs", params={"target_type": "workflow"}).json()["total"] == 1
    assert client.get("/api/schedule/runs", params={"status": "ok"}).json()["total"] == 1
    assert client.get("/api/schedule/runs", params={"task_id": "st-a"}).json()["total"] == 1
    trig = client.get("/api/schedule/runs", params={"trigger": "manual"}).json()["rows"][0]
    assert trig["run_id"] == "sr-2"
    page = client.get("/api/schedule/runs", params={"page": 2, "page_size": 1}).json()
    assert page["total"] == 2 and len(page["rows"]) == 1


def test_runs_seen_endpoint_and_unseen_payload(client):
    """schedule-unseen：GET /api/schedule 带 unseen 摘要；POST runs/seen 标
    单条/全部；rows 每行带 seen 标记、支持 seen 过滤。"""
    now = time.time()
    for rid, st in [("sr-1", "ok"), ("sr-2", "error"), ("sr-3", "missed")]:
        store.record_run({
            "run_id": rid, "task_id": "st-a", "task_name": "A",
            "target_type": "prompt", "trigger": "schedule", "status": st,
            "scheduled_at": now, "started_at": now, "finished_at": now,
        })
    # GET /api/schedule 的 unseen 摘要（侧栏红点数据源）
    payload = client.get("/api/schedule").json()
    assert payload["unseen"] == {"total": 3, "error": 1, "missed": 1}
    # rows 默认带 seen=false
    rows = client.get("/api/schedule/runs").json()["rows"]
    assert all(r["seen"] is False for r in rows)
    # 标单条 → 摘要减一，seen 过滤生效
    r = client.post("/api/schedule/runs/seen", json={"run_id": "sr-2"}).json()
    assert r["ok"] and r["unseen"] == {"total": 2, "error": 0, "missed": 1}
    assert client.get("/api/schedule/runs", params={"seen": "unseen"}).json()["total"] == 2
    assert client.get("/api/schedule/runs", params={"seen": "seen"}).json()["rows"][0]["run_id"] == "sr-2"
    # 一键全部已读 → 归零
    r = client.post("/api/schedule/runs/seen", json={"all": True}).json()
    assert r["unseen"] == {"total": 0, "error": 0, "missed": 0}
    assert client.get("/api/schedule").json()["unseen"]["total"] == 0
    # 空 body → 400
    assert client.post("/api/schedule/runs/seen", json={}).status_code == 400


def test_timeline_runs_and_planned(client):
    interval = {"kind": "interval", "minutes": 30}
    t = client.post("/api/schedule/tasks", json=_prompt_task(schedule=interval)).json()
    today = time.strftime("%Y-%m-%d", time.localtime())
    r = client.get("/api/schedule/timeline", params={"date": today}).json()
    assert r["date"] == today and r["runs"] == []
    # 时间炸弹修复（2026-10-04）：23:30 后当天 30 分钟网格的剩余点可能为 0
    # （下一点落到明天）——当天为空时改查明天，验证语义不变：网格步进+全在未来。
    if not r["planned"]:
        tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        r = client.get("/api/schedule/timeline", params={"date": tomorrow}).json()
        assert r["date"] == tomorrow and r["runs"] == []
    # interval 任务：剩余计划点按网格步进，至少 1 个且都在未来
    assert len(r["planned"]) >= 1
    assert all(p["task_id"] == t["id"] and p["at"] > time.time() for p in r["planned"])
    planned_ats = [p["at"] for p in r["planned"]]
    assert planned_ats == sorted(planned_ats)
    gaps = {round(b - a) for a, b in zip(planned_ats, planned_ats[1:], strict=False)}
    assert gaps == {1800}
    # 全局关 → 无计划点
    client.put("/api/schedule", json={"enabled": False})
    r2 = client.get("/api/schedule/timeline", params={"date": today}).json()
    assert r2["planned"] == []


def test_timeline_bad_date_400(client):
    assert client.get("/api/schedule/timeline", params={"date": "not-a-date"}).status_code == 400


# ---- WS 事件通道 ---------------------------------------------------------------
def test_ws_snapshot_and_events(client):
    with client.websocket_connect("/api/ws/schedule") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert "enabled" in snap and "tasks" in snap
        # 页面打开期间调度器/REST 发的事件实时到达
        scheduler.schedule_events().emit("run_finished", {"run": {"run_id": "sr-live"}})
        msg = ws.receive_json()
        assert msg["type"] == "run_finished"
        assert msg["run"]["run_id"] == "sr-live"
        scheduler.schedule_events().emit("missed", {"task_id": "st-1"})
        assert ws.receive_json()["type"] == "missed"


# ---- workflow 目标 e2e（headless run + origin 徽标 + 摘要）----------------------
def test_workflow_target_end_to_end(client, monkeypatch):
    """验收 §8/§11/§12/§13：定时触发生成无父会话 run（origin=schedule）、执行记录
    running→ok、LLM 摘要成功后以同 run_id 末行替位、摘要调用计入 usage
    source=schedule。"""
    from ginno_runtime import usage_store
    from ginno_runtime.testing.fake_model import ScriptedChatModel
    from ginno_runtime.workflows import store as wf_store

    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
             "input_token_details": {"cache_read": 40, "cache_creation": 0}}
    shared_model = ScriptedChatModel(scripts=[
        script('体检完成\nWRITE_JSON {"ok": true}', usage=usage),
        # 第二次调用 = 摘要调用
        script("体检通过，未发现阻塞问题。", usage=usage),
    ])

    def fake_build_model(*a, **k):
        # 同一实例：第 1 次调用 = workflow 步，第 2 次 = 摘要调用
        return shared_model

    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", fake_build_model)
    monkeypatch.setattr("ginno_runtime.models.build_model", fake_build_model)

    wf = _wf_def(wf_store)
    body = {
        "name": "仓库体检",
        "target": {"type": "workflow", "workflow_id": wf["id"], "context_override": {}},
        "schedule": {"kind": "daily", "at": "02:00"},
    }
    task = client.post("/api/schedule/tasks", json=body).json()

    # 手动触发走同一执行路径（_execute_run → _run_workflow_target）
    run_id = client.post(f"/api/schedule/tasks/{task['id']}/run").json()["run_id"]
    # 轮询执行记录到终态（后台协程在 app loop 里跑）
    deadline = time.time() + 20
    rows = []
    while time.time() < deadline:
        rows = store.dedupe_runs(store.load_day(store._today()))
        if rows and rows[0]["status"] != "running":
            break
        time.sleep(0.2)
    row = rows[0]
    assert row["run_id"] == run_id
    assert row["status"] == "ok", row
    assert row["target_type"] == "workflow"
    assert row["workflow_run_id"]
    assert row["session_id"] is None  # 无父会话

    # run 记录带 origin=schedule（Workflows 页 ⏰ 徽标的数据源）
    wf_run = wf_store.get_run(row["workflow_run_id"])
    assert wf_run["origin"] == "schedule"
    assert wf_run["session_id"] is None

    # 摘要：LLM 末行替位（≤160 字）
    deadline = time.time() + 10
    summary = row["summary"]
    while time.time() < deadline:
        rows = store.dedupe_runs(store.load_day(store._today()))
        if rows[0]["summary"] == "体检通过，未发现阻塞问题。":
            summary = rows[0]["summary"]
            break
        time.sleep(0.2)
    assert summary == "体检通过，未发现阻塞问题。"
    assert len(summary) <= 160

    # usage：workflow 本体 source=workflow；摘要调用 source=schedule（§4.4/§13）
    entries = usage_store.load_day(usage_store._today())
    sources = {e["source"] for e in entries}
    assert "workflow" in sources
    assert "schedule" in sources
    sched_rows = [e for e in entries if e["source"] == "schedule"]
    assert all(e["turn_id"] == run_id for e in sched_rows)


def test_workflow_target_missing_def_records_error(client):
    """验收 §9：配方删除后到点记 error("workflow_missing")。"""
    body = _prompt_task(target={"type": "workflow", "workflow_id": "gone"})
    task = client.post("/api/schedule/tasks", json=body).json()
    run_id = client.post(f"/api/schedule/tasks/{task['id']}/run").json()["run_id"]
    deadline = time.time() + 10
    rows = []
    while time.time() < deadline:
        rows = store.dedupe_runs(store.load_day(store._today()))
        if rows and rows[0]["status"] != "running":
            break
        time.sleep(0.1)
    assert rows[0]["run_id"] == run_id
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "workflow_missing"
