"""API tests for /api/tasks/overview —— 六源只读聚合 + 统一停止门面。

六源各造一条（fake turn / 调度任务 / plan / workflow run / goal / delegation）
断言枚举与统一句柄形状；stop 门面用 monkeypatch 替换各域 handler 验证纯路由
分发（不真停），schedule 暂停（enabled=false，非删除）走真实路径验证语义。
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from ginno_runtime import schedule_store, server_shared
from ginno_runtime.goals import store as goal_store
from ginno_runtime.plans import store as plan_store
from ginno_runtime.session_meta import _session_meta_upsert
from ginno_runtime.tools.external_agent import _ACTIVE_DELEGATIONS
from ginno_runtime.workflows import store as wf_store

pytestmark = pytest.mark.api

_NOW = time.time()


@pytest.fixture(autouse=True)
def _clean_global_state():
    """conftest 只清 _RUNNING_TURNS；parked 集合与委托表本文件自查自清。"""
    server_shared._PENDING_RESUME.clear()
    server_shared._PENDING_KIND.clear()
    _ACTIVE_DELEGATIONS.clear()
    yield
    server_shared._PENDING_RESUME.clear()
    server_shared._PENDING_KIND.clear()
    _ACTIVE_DELEGATIONS.clear()


def _seed_session(sid: str, *, type: str = "", title: str | None = None, **kw) -> None:
    meta = {
        "id": sid,
        "title": title or sid,
        "type": type,
        "created": _NOW,
        "updated": _NOW,
    }
    meta.update(kw)
    _session_meta_upsert("default", meta)


def _arm_turn(sid: str, turn_id: str = "t-1") -> None:
    server_shared._RUNNING_TURNS[sid] = turn_id
    server_shared._RUNNING_TURNS_AT[sid] = _NOW


def _park_turn(sid: str, kind: str = "user_question") -> None:
    server_shared._PENDING_RESUME.add(sid)
    server_shared._PENDING_KIND[sid] = kind


def _seed_schedule_task(task_id: str = "st-1", name: str = "每日趋势分析") -> None:
    schedule_store.save_config(
        {
            "enabled": True,
            "tasks": [
                {
                    "id": task_id,
                    "name": name,
                    "enabled": True,
                    "schedule": {"kind": "interval", "minutes": 60},
                    "target": {"type": "prompt", "prompt": "hi"},
                    "next_run_at": _NOW + 600,
                }
            ],
        }
    )


def _seed_plan(session_id: str = "s-plan") -> str:
    _seed_session(session_id)
    plan = plan_store.create_plan(
        session_id,
        slug="default",
        title="重构批次 8",
        items=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
    )
    return plan["plan_id"]


def _seed_run(present_in: str = "s-run-host") -> str:
    _seed_session(present_in)
    run = wf_store.create_run(
        {"id": "wf-1", "name": "周报流水线", "dsl": {"steps": []}},
        session_id=present_in,
        present_in_session_id=present_in,
    )
    return run["id"]


def _seed_goal(sid: str = "s-goal") -> None:
    _seed_session(sid, title="发版窗口")
    goal_store.create_goal("default", sid, "完成发版")


def _seed_delegation(did: str = "did-1") -> None:
    _seed_session("s-parent", title="主会话")
    _seed_session(
        did,
        type="delegation",
        title="codex · 修复登录超时",
        stop_reason="running",
        parent_session_id="s-parent",
    )
    _ACTIVE_DELEGATIONS[did] = {"session_id": "s-parent", "proc": object()}


def _get_overview(client) -> dict:
    r = client.get("/api/tasks/overview")
    assert r.status_code == 200
    return r.json()


# ---- 空态 ---------------------------------------------------------------------


def test_overview_empty_returns_empty_groups(client):
    data = _get_overview(client)
    assert data["ok"] is True
    for group in ("turns", "scheduled", "subagents", "runs", "goals", "delegations"):
        assert data[group] == [], group
    assert data["counts"]["total"] == 0
    assert data["counts"]["phases"] == {"running": 0, "waiting": 0, "scheduled": 0}


# ---- 六源枚举 -----------------------------------------------------------------


def test_overview_enumerates_all_six_sources(client):
    _seed_session("s-turn", title="ginno 开发区")
    _arm_turn("s-turn")
    _seed_schedule_task()
    plan_id = _seed_plan()
    run_id = _seed_run()
    _seed_goal()
    _seed_delegation()

    data = _get_overview(client)
    assert data["counts"] == {
        "turns": 1, "scheduled": 1, "subagents": 1, "runs": 1,
        "goals": 1, "delegations": 1, "total": 6,
        "phases": {"running": 5, "waiting": 0, "scheduled": 1},
    }

    (turn,) = data["turns"]
    assert turn["kind"] == "turn" and turn["id"] == "s-turn"
    assert turn["phase"] == "running"  # 三档分类：活 turn → 执行中
    assert turn["title"] == "ginno 开发区"
    assert turn["session_id"] == "s-turn" and turn["session_title"] == "ginno 开发区"
    assert turn["started_at"] == _NOW and turn["stoppable"] is True
    assert "detail" in turn and "stop_hint" not in turn

    (sched,) = data["scheduled"]
    assert sched["kind"] == "schedule" and sched["id"] == "st-1"
    assert sched["phase"] == "scheduled"  # 三档分类：调度 → 已排期
    assert sched["title"] == "每日趋势分析"
    assert sched["next_fire_at"] == _NOW + 600
    assert sched["detail"] == "每 60 分钟"  # 人读摘要（seed=interval；旧口径是 target.type 噪音）
    assert sched["stoppable"] is True and sched["stop_hint"]

    (plan,) = data["subagents"]
    assert plan["kind"] == "plan" and plan["id"] == plan_id
    assert plan["phase"] == "running"
    assert plan["title"] == "重构批次 8"
    assert plan["session_id"] == "s-plan"
    assert plan["detail"] == "0 done / 0 running / 3 items"

    (run,) = data["runs"]
    assert run["kind"] == "run" and run["id"] == run_id
    assert run["phase"] == "running"
    assert run["title"] == "周报流水线" and run["session_id"] == "s-run-host"

    (goal,) = data["goals"]
    assert goal["kind"] == "goal" and goal["id"] == "s-goal"
    assert goal["phase"] == "running"
    assert goal["title"] == "完成发版" and goal["session_title"] == "发版窗口"
    assert "推进" in goal["detail"]  # 新口径：相对时间主信号（turns=0 不附 turn 0）

    (dele,) = data["delegations"]
    assert dele["kind"] == "delegation" and dele["id"] == "did-1"
    assert dele["phase"] == "running"
    assert dele["title"] == "codex · 修复登录超时"
    assert dele["session_id"] == "s-parent" and dele["stoppable"] is True


def test_overview_excludes_non_active_goal_and_disabled_schedule(client):
    """paused goal / 任务级 disabled / 全局 disabled 不算「正在运行」。"""
    _seed_goal("s-goal-paused")
    goal_store.update_status("default", "s-goal-paused", goal_store.STATUS_PAUSED)
    schedule_store.save_config(
        {"enabled": False, "tasks": [{"id": "st-x", "name": "x", "enabled": True}]}
    )
    data = _get_overview(client)
    assert data["goals"] == []
    assert data["scheduled"] == []


def test_overview_skips_delegation_placeholder_slots(client):
    """并发闸门的预占槽（proc=None、uuid key）不是真实委托。"""
    _ACTIVE_DELEGATIONS["slot-uuid"] = {"session_id": "s-parent", "proc": None}
    data = _get_overview(client)
    assert data["delegations"] == []


# ---- stoppable 判定 ------------------------------------------------------------


def test_parked_turn_is_not_stoppable(client):
    _seed_session("s-parked", title="SOPFlow 调研")
    _arm_turn("s-parked")
    _park_turn("s-parked", "user_question")
    data = _get_overview(client)
    (turn,) = data["turns"]
    assert turn["stoppable"] is False
    assert turn["stop_hint"] == "等待你回答，请到会话中处理"
    # 三档分类：parked turn → waiting（等人），与 running turn 同组不同档
    assert turn["phase"] == "waiting"
    assert data["counts"]["phases"] == {"running": 0, "waiting": 1, "scheduled": 0}
    # POST stop 同语义拒停（透传不可停语义，不 heal 挂起 turn）
    r = client.post(
        "/api/tasks/overview/stop", json={"kind": "turn", "id": "s-parked"}
    )
    assert r.status_code == 409
    assert r.json()["stop_hint"]


def test_parked_turn_without_running_flag_still_listed(client):
    """parked 但 turn 已从 _RUNNING_TURNS 弹出（stop 分支语义）仍要枚举。"""
    _seed_session("s-only-parked")
    _park_turn("s-only-parked", "permission_request")
    data = _get_overview(client)
    (turn,) = data["turns"]
    assert turn["id"] == "s-only-parked"
    assert turn["stoppable"] is False
    assert turn["phase"] == "waiting"


# ---- stop 门面：纯路由分发 -------------------------------------------------------


def test_stop_facade_dispatches_to_domain_handlers(client, monkeypatch):
    """六 kind 各路由到既有 handler；门面自身零停止逻辑（全 mock 即全绿）。"""
    from ginno_runtime.api.plans import cancel_plan_endpoint
    from ginno_runtime.api.schedule import patch_task
    from ginno_runtime.api.sessions.delegations import stop_delegation, stop_session
    from ginno_runtime.api.sessions.goal import clear_session_goal
    from ginno_runtime.api.workflows import cancel_workflow_run_endpoint

    stop_session_m = AsyncMock(return_value={"stopped": True, "mode": "running"})
    patch_task_m = AsyncMock(return_value={"id": "st-1", "enabled": False})
    cancel_plan_m = AsyncMock(return_value={"ok": True})
    cancel_run_m = AsyncMock(return_value={"ok": True, "status": "cancelled"})
    clear_goal_m = AsyncMock(return_value={"ok": True, "cleared": True})
    stop_dele_m = AsyncMock(return_value={"ok": True, "killed": True})
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.delegations.stop_session", stop_session_m
    )
    monkeypatch.setattr("ginno_runtime.api.schedule.patch_task", patch_task_m)
    monkeypatch.setattr(
        "ginno_runtime.api.plans.cancel_plan_endpoint", cancel_plan_m
    )
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.cancel_workflow_run_endpoint", cancel_run_m
    )
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.goal.clear_session_goal", clear_goal_m
    )
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.delegations.stop_delegation", stop_dele_m
    )

    def _stop(body: dict):
        r = client.post("/api/tasks/overview/stop", json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is True and out["kind"] == body["kind"]
        return out

    _stop({"kind": "turn", "id": "s-1"})
    stop_session_m.assert_awaited_once_with("s-1")

    _stop({"kind": "schedule", "id": "st-1"})
    patch_task_m.assert_awaited_once_with("st-1", data={"enabled": False})

    _stop({"kind": "plan", "id": "pl-1", "session_id": "s-plan"})
    cancel_plan_m.assert_awaited_once_with("s-plan", "pl-1")

    _stop({"kind": "run", "id": "run-1"})
    cancel_run_m.assert_awaited_once_with("run-1")

    _stop({"kind": "goal", "id": "s-goal"})
    clear_goal_m.assert_awaited_once_with("s-goal")

    _stop({"kind": "delegation", "id": "did-1"})
    stop_dele_m.assert_awaited_once_with("did-1")


def test_stop_unknown_kind_is_400(client):
    r = client.post("/api/tasks/overview/stop", json={"kind": "nope", "id": "x"})
    assert r.status_code == 400


def test_stop_plan_requires_session_id(client):
    r = client.post("/api/tasks/overview/stop", json={"kind": "plan", "id": "pl-1"})
    assert r.status_code == 400
    assert "session_id" in r.json()["error"]


def test_stop_unknown_target_passes_through_domain_404(client):
    """门面不吞各域校验：不存在的 run 透传 workflow 域的 404。"""
    r = client.post("/api/tasks/overview/stop", json={"kind": "run", "id": "nope"})
    assert r.status_code == 404


# ---- schedule 停止语义：暂停而非删除 --------------------------------------------


def test_stop_schedule_pauses_without_deleting(client):
    _seed_schedule_task()
    r = client.post(
        "/api/tasks/overview/stop", json={"kind": "schedule", "id": "st-1"}
    )
    assert r.status_code == 200
    task = schedule_store.get_task("st-1")
    assert task is not None, "暂停不删除任务"
    assert task["enabled"] is False
    data = _get_overview(client)
    assert data["scheduled"] == [], "暂停后不再枚举为运行中"


@pytest.mark.parametrize(("turns", "seconds"), [("unknown", 10), (2, {})])
def test_bad_goal_usage_does_not_break_other_groups(client, turns, seconds):
    _seed_goal()
    _seed_schedule_task()
    goals = goal_store.list_goals("default")
    goals["s-goal"].update(turns_used=turns, time_used_seconds=seconds)
    goal_store._write("default", goals)
    response = client.get("/api/tasks/overview")
    assert response.status_code == 200
    assert response.json()["goals"][0]["id"] == "s-goal"
    assert response.json()["scheduled"][0]["id"] == "st-1"
