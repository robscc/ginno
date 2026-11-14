"""P2-1：schedule 驱动团队（backlog P2-1）。

此前 TARGET_TYPES 只有 prompt/workflow，prompt 目标走影子会话 headless
**绕过团队路由** —— 「每天 9 点团队站会」无法成立。
"""

from __future__ import annotations

import pytest

from ginno_runtime import schedule_store
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


def _model(*texts):
    return ScriptedChatModel(scripts=[script(text=t) for t in texts])


def test_team_target_type_accepted():
    assert "team" in schedule_store.TARGET_TYPES


def test_team_target_validation():
    """team 目标必须有 team_id 与要投递的消息。"""
    base = {"name": "站会", "schedule": {"kind": "daily", "at": "09:00"}, "enabled": True}
    # 缺 team_id
    errs = schedule_store.validate_task({
        **base, "target": {"type": "team", "prompt": "开个站会"},
    })
    assert any("team" in str(e).lower() for e in errs), errs
    # 缺 prompt
    errs = schedule_store.validate_task({
        **base, "target": {"type": "team", "team_id": "t_x"},
    })
    assert errs, "缺消息应报错"
    # 齐全 → 通过
    errs = schedule_store.validate_task({
        **base, "target": {"type": "team", "team_id": "t_x", "prompt": "开个站会"},
    })
    assert not errs, errs


def test_prompt_and_workflow_targets_unchanged():
    """放开 team 不得破坏既有两类目标的校验。"""
    base = {"name": "x", "schedule": {"kind": "daily", "at": "09:00"}, "enabled": True}
    assert not schedule_store.validate_task({
        **base, "target": {"type": "prompt", "prompt": "hi"},
    })
    assert schedule_store.validate_task({
        **base, "target": {"type": "prompt", "prompt": ""},
    })
    assert schedule_store.validate_task({
        **base, "target": {"type": "bogus"},
    })


async def test_run_team_target_delivers_to_leader(client, patch_build_model):
    """真实执行：缺省投领队，走 teams.deliver（不是影子会话）。"""
    from ginno_runtime import scheduler

    patch_build_model(_model("站会纪要：一切正常"))
    t = client.post("/api/teams", json={
        "name": "定时队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    leader_iid = t["leader_instance_id"]

    task = {
        "id": "task_1", "name": "每日站会",
        "target": {"type": "team", "team_id": t["id"], "prompt": "开个站会",
                   "timeout_s": 20},
    }
    row = {"run_id": "r1", "started_at": 0.0}
    out = await scheduler._run_team_target(task, row)
    assert out["ok"], out
    # 领队实例真的跑了（状态回流由 lifecycle 钩子落盘）
    from ginno_runtime.teams import store as team_store

    m = team_store.get_member(t["id"], leader_iid)
    assert m["status"] in ("done", "idle", "working"), m


async def test_run_team_target_explicit_member(client, patch_build_model):
    """指定 instance_id 时投该成员，而不是领队。"""
    from ginno_runtime import scheduler
    from ginno_runtime.teams import store as team_store

    patch_build_model(_model("巡检完成"))
    t = client.post("/api/teams", json={
        "name": "巡检队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    m = client.post(f"/api/teams/{t['id']}/members",
                    json={"agent_id": "writer"}).json()["member"]

    task = {
        "id": "task_2", "name": "巡检",
        "target": {"type": "team", "team_id": t["id"],
                   "instance_id": m["instance_id"], "prompt": "巡检 CI",
                   "timeout_s": 20},
    }
    out = await scheduler._run_team_target(task, {"run_id": "r2", "started_at": 0.0})
    assert out["ok"], out
    assert team_store.get_member(t["id"], m["instance_id"])["status"] in (
        "done", "idle", "working"
    )


async def test_run_team_target_unknown_team_fails_cleanly():
    from ginno_runtime import scheduler

    out = await scheduler._run_team_target(
        {"id": "t", "name": "x",
         "target": {"type": "team", "team_id": "nope", "prompt": "hi"}},
        {"run_id": "r", "started_at": 0.0},
    )
    assert out["ok"] is False
    assert out["error"]


async def test_run_team_target_empty_team_fails_cleanly(client, patch_build_model):
    from ginno_runtime import scheduler

    patch_build_model(_model("ok"))
    t = client.post("/api/teams", json={"name": "空队", "project_slug": "default"}).json()["team"]
    out = await scheduler._run_team_target(
        {"id": "t", "name": "x",
         "target": {"type": "team", "team_id": t["id"], "prompt": "hi"}},
        {"run_id": "r", "started_at": 0.0},
    )
    assert out["ok"] is False
