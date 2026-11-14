"""P2-4：workflow 节点调用团队成员（backlog P2-4）。

节点带 team_member 时不跑进程内 goal→tools 循环，而是把 goal 投给常驻成员
实例并等回帖——团队的流水线/并行/分支/闸门由此复用 workflow DSL 既有原语。
"""

from __future__ import annotations

import pytest

from ginno_runtime.teams import store as team_store
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows.nodes import builtin

pytestmark = pytest.mark.api


def _model(*texts):
    return ScriptedChatModel(scripts=[script(text=t) for t in texts])


def test_agent_node_schema_accepts_team_member():
    schema = builtin.AgentNode.params_schema
    assert "team_member" in schema["properties"]
    tm = schema["properties"]["team_member"]
    assert tm["required"] == ["team_id"]
    assert set(tm["properties"]) >= {"team_id", "instance_id", "timeout_s"}


async def test_team_member_node_delivers_and_returns_member_output(client, patch_build_model):
    """真实执行：节点 goal 投给成员，返回成员产出作为节点结果。"""
    patch_build_model(_model("成员产出：仓库干净，无未提交改动"))
    t = client.post("/api/teams", json={
        "name": "流程队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    iid = t["leader_instance_id"]

    node = {
        "id": "n1", "type": "step", "goal": "盘点仓库状态",
        "team_member": {"team_id": t["id"], "instance_id": iid, "timeout_s": 20},
    }
    text, usage, warn = await builtin._run_team_member_turn(
        node, node["team_member"], "盘点仓库状态", None, "n1",
    )
    assert "成员产出" in text, text
    assert warn is None
    assert set(usage) >= {"input_tokens", "output_tokens"}
    # 成员真的跑了（状态回流由 lifecycle 钩子落盘）
    m = team_store.get_member(t["id"], iid)
    assert m["status"] in ("done", "idle", "working"), m


async def test_team_member_node_defaults_to_leader(client, patch_build_model):
    """instance_id 缺省 → 投领队。"""
    patch_build_model(_model("领队产出"))
    t = client.post("/api/teams", json={
        "name": "缺省队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    node = {"id": "n", "type": "step", "goal": "干活",
            "team_member": {"team_id": t["id"], "timeout_s": 20}}
    text, _usage, _warn = await builtin._run_team_member_turn(
        node, node["team_member"], "干活", None, "n",
    )
    assert text
    assert team_store.get_member(t["id"], t["leader_instance_id"])["status"] in (
        "done", "idle", "working"
    )


async def test_team_member_node_unknown_team_raises():
    with pytest.raises(ValueError, match="unknown team"):
        await builtin._run_team_member_turn(
            {"id": "n"}, {"team_id": "nope"}, "goal", None, "n",
        )


async def test_team_member_node_empty_team_raises(client, patch_build_model):
    patch_build_model(_model("ok"))
    t = client.post("/api/teams", json={"name": "空队", "project_slug": "default"}).json()["team"]
    with pytest.raises(ValueError, match="no members"):
        await builtin._run_team_member_turn(
            {"id": "n"}, {"team_id": t["id"]}, "goal", None, "n",
        )


async def test_team_member_node_unknown_instance_raises(client, patch_build_model):
    patch_build_model(_model("ok"))
    t = client.post("/api/teams", json={
        "name": "队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    with pytest.raises(ValueError, match="unknown instance"):
        await builtin._run_team_member_turn(
            {"id": "n"}, {"team_id": t["id"], "instance_id": "ai_ghost"}, "goal", None, "n",
        )


async def test_agent_turn_routes_to_team_mode_when_field_present(client, patch_build_model):
    """_run_agent_turn 顶部分支：带 team_member 就走成员模式（两条路径共用）。"""
    patch_build_model(_model("分支命中"))
    t = client.post("/api/teams", json={
        "name": "分支队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    node = {
        "id": "n1", "type": "step", "goal": "走成员模式",
        "team_member": {"team_id": t["id"], "timeout_s": 20},
    }
    cctx = {"run_ctx": None, "model": None, "tools": []}
    text, _usage, _warn = await builtin._run_agent_turn(node, cctx, {}, {}, lambda *a, **k: None)
    assert "分支命中" in text
