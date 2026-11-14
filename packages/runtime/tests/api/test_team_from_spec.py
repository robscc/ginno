"""模板 / 一句话建队（from_spec）全流程回归（backlog P0-6）。

此前 tests/api/test_team_templates.py **只测 spec 解析，无建队断言**，
导致三处生产断裂全部漏网：
  ① first_task 投递不带目标 → 必 no_target 失败，开场任务静默丢弃
  ② 建队已 appoint_leader，循环再加 leader 被拒 → member_errors 每次必含领队，
     且领队显示名变成主会话标题「👥 队名」而非 spec 名
  ③ spec.members[].instructions 无落点 → 内阁「反糊弄三件套」到不了成员

本文件走**真实端点 + 真实记忆落盘**，不 mock 消费方。
"""

from __future__ import annotations

import pytest

from ginno_runtime.teams import memory as team_memory
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


def _model(*texts):
    return ScriptedChatModel(scripts=[script(text=t) for t in texts])


def _spec(**over):
    """一个内阁式 spec：1 领队 + 2 成员，各带 instructions。"""
    base = {
        "name": "测试内阁",
        "charter_md": "# 章程\n回报必须附证据。",
        "project_slug": "default",
        "members": [
            {"agent_id": "dev", "display_name": "首辅", "role": "leader",
             "instructions": "回报纪律：报完成必须附路径、行号、命令输出与未验证清单。"},
            {"agent_id": "research", "display_name": "调研员", "role": "member",
             "instructions": "只读调研，结论必须带来源。"},
            {"agent_id": "writer", "display_name": "文书", "role": "member",
             "instructions": "产出必须可直接使用。"},
        ],
        "first_task": "先盘点当前仓库状态，给我一份清单。",
    }
    base.update(over)
    return base


def test_from_spec_no_member_errors(client, patch_build_model):
    """② 领队不得被重复任命 → member_errors 必须为空。"""
    patch_build_model(_model("ok"))
    r = client.post("/api/teams/from_spec", json=_spec()).json()
    assert r["ok"], r
    assert r["member_errors"] == [], f"领队被重复任命：{r['member_errors']}"
    # 3 个成员全部落地（领队 1 + 成员 2），不因跳过而丢人
    assert len(r["team"]["members"]) == 3


def test_from_spec_leader_keeps_spec_display_name(client, patch_build_model):
    """② 领队显示名必须是 spec 名，不是主会话标题「👥 队名」。"""
    patch_build_model(_model("ok"))
    r = client.post("/api/teams/from_spec", json=_spec()).json()
    assert r["ok"], r
    leader_id = r["team"]["leader_instance_id"]
    leader = next(m for m in r["team"]["members"] if m["instance_id"] == leader_id)
    assert leader["display_name"] == "首辅"
    assert not leader["display_name"].startswith("👥")


def test_from_spec_instructions_reach_member_notes(client, patch_build_model):
    """③ instructions 必须落到实例 notes.md（经既有记忆注入到成员每个 turn）。"""
    patch_build_model(_model("ok"))
    r = client.post("/api/teams/from_spec", json=_spec()).json()
    assert r["ok"], r
    team_id = r["team"]["id"]
    for m in r["team"]["members"]:
        notes = team_memory.member_notes(team_id, m["instance_id"])
        assert notes.strip(), f"{m['display_name']} 的 instructions 丢失（P0-6）"
    # 领队的「反糊弄」契约确实到位
    leader_id = r["team"]["leader_instance_id"]
    assert "未验证清单" in team_memory.member_notes(team_id, leader_id)
    # 普通成员的职责也到位
    researcher = next(m for m in r["team"]["members"] if m["agent_id"] == "research")
    assert "只读调研" in team_memory.member_notes(team_id, researcher["instance_id"])


def test_from_spec_first_task_actually_delivered(client, patch_build_model):
    """① first_task 必须真的投递成功（此前必 no_target 失败）。"""
    patch_build_model(_model("盘点完成：仓库干净"))
    r = client.post("/api/teams/from_spec", json=_spec()).json()
    assert r["ok"], r
    assert r["first_task_delivered"] is True, "开场任务未投递（P0-6 ①）"


def test_from_spec_without_first_task_is_none(client, patch_build_model):
    """无开场任务时 first_task_delivered 应为 None（不是 False）。"""
    patch_build_model(_model("ok"))
    r = client.post("/api/teams/from_spec", json=_spec(first_task="")).json()
    assert r["ok"], r
    assert r["first_task_delivered"] is None


def test_from_spec_charter_written(client, patch_build_model):
    """章程写进 TEAM.md（原有行为，防回归）。"""
    patch_build_model(_model("ok"))
    r = client.post("/api/teams/from_spec", json=_spec()).json()
    assert r["ok"], r
    assert "回报必须附证据" in team_memory.team_md(r["team"]["id"])


def test_from_spec_explicit_other_leader_keeps_spec_leader_as_member(client, patch_build_model):
    """调用方用 agent_id 指定**别的**领队时：spec 领队降级为成员加入，不丢人。

    此前该路径下 spec 领队仍带 role="leader" → 被「一队一领队」拒绝 →
    整个成员丢失。且不能把 spec 领队的名字安到实际领队头上。
    """
    patch_build_model(_model("ok"))
    r = client.post("/api/teams/from_spec", json=_spec(agent_id="writer")).json()
    assert r["ok"], r
    assert r["member_errors"] == [], r["member_errors"]
    members = r["team"]["members"]
    # writer 当领队 + spec 三个成员（dev/research/writer）全部落地 = 4 个实例
    assert len(members) == 4
    assert {m["agent_id"] for m in members} == {"dev", "research", "writer"}
    # 只有一个领队，且是 writer
    leaders = [m for m in members if m["role"] == "leader"]
    assert len(leaders) == 1 and leaders[0]["agent_id"] == "writer"
    # 领队的名字不得挪用 spec 领队（dev）的「首辅」
    assert leaders[0]["display_name"] != "首辅"
    # spec 领队 dev 降级为成员，且其职责说明仍到位
    dev = next(m for m in members if m["agent_id"] == "dev")
    assert dev["role"] == "member" and dev["display_name"] == "首辅"
    assert "未验证清单" in team_memory.member_notes(r["team"]["id"], dev["instance_id"])
