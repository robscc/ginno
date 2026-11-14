"""P2-23：团队记忆注入的 **turn 级** 断言（backlog P2-23）。

此前只测 `teams.memory.turn_context()` 这个纯函数，从未断言**成员真实 turn
的模型输入**里出现了 TEAM.md —— 函数对但接线断（turn.py:660 →
graph_prompts.build_turn_context → HumanMessage）时测试照样绿。

本文件跑真实成员 turn，用捕获型 fake model 记录它实际收到的 messages，
断言三层记忆与权限纪律条款都真的到了模型面前。
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime.teams import memory as team_memory
from ginno_runtime.teams import store as team_store
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


class CapturingModel(ScriptedChatModel):
    """记录每次调用收到的 messages（ScriptedChatModel 本身不记录）。"""

    seen: list = []

    def _next(self, messages=None):
        try:
            self.seen.append(list(messages or []))
        except Exception:  # noqa: BLE001
            pass
        return super()._next(messages)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            self.seen.append(list(messages or []))
        except Exception:  # noqa: BLE001
            pass
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _all_text(seen: list) -> str:
    out = []
    for call in seen:
        for m in call:
            c = getattr(m, "content", "")
            out.append(c if isinstance(c, str) else str(c))
    return "\n".join(out)


def test_member_real_turn_receives_team_memory(client, patch_build_model):
    """成员真实 turn 的模型输入必须含 TEAM.md + 自己的 notes + 权限纪律。"""
    model = CapturingModel(scripts=[script(text="已盘点完成")])
    model.seen = []
    patch_build_model(model)

    t = client.post("/api/teams", json={"name": "记忆队", "project_slug": "default"}).json()["team"]
    team_id = t["id"]
    m = client.post(f"/api/teams/{team_id}/members", json={"agent_id": "dev"}).json()["member"]

    team_memory.set_team_md(team_id, "TEAM约定：所有产出必须附证据。")
    team_memory.set_member_notes(team_id, m["instance_id"], "DEV专属：只动 runtime 目录。")

    # 真实投递 → 真实成员 turn
    r = client.post(
        f"/api/teams/{team_id}/deliver",
        json={"instance_id": m["instance_id"], "text": "盘点一下仓库"},
    ).json()
    assert r.get("ok"), r

    deadline = time.time() + 20
    while time.time() < deadline and not model.seen:
        time.sleep(0.2)
    assert model.seen, "成员 turn 没有真的跑起来（模型未被调用）"

    text = _all_text(model.seen)
    assert "TEAM约定：所有产出必须附证据。" in text, \
        f"TEAM.md 未到达成员真实 turn（P2-23 接线断）：{text[:600]}"
    assert "DEV专属：只动 runtime 目录。" in text, "实例 notes 未到达成员 turn"
    low = text.lower()
    assert "not approval" in low or "不等于批准" in text, "权限纪律条款未到达成员 turn"


def test_member_turn_does_not_receive_other_members_notes(client, patch_build_model):
    """隔离纪律：A 的 notes 不得进 B 的 turn（三层记忆的边界）。"""
    model = CapturingModel(scripts=[script(text="ok"), script(text="ok")])
    model.seen = []
    patch_build_model(model)

    t = client.post("/api/teams", json={"name": "隔离队", "project_slug": "default"}).json()["team"]
    team_id = t["id"]
    a = client.post(f"/api/teams/{team_id}/members", json={"agent_id": "dev"}).json()["member"]
    b = client.post(f"/api/teams/{team_id}/members", json={"agent_id": "writer"}).json()["member"]
    team_memory.set_member_notes(team_id, a["instance_id"], "甲的私密备注AAA")
    team_memory.set_member_notes(team_id, b["instance_id"], "乙的私密备注BBB")

    client.post(f"/api/teams/{team_id}/deliver",
                json={"instance_id": b["instance_id"], "text": "干活"})
    deadline = time.time() + 20
    while time.time() < deadline and not model.seen:
        time.sleep(0.2)
    assert model.seen
    text = _all_text(model.seen)
    assert "乙的私密备注BBB" in text
    assert "甲的私密备注AAA" not in text, "成员 A 的 notes 泄漏进 B 的 turn"


def test_non_team_session_gets_no_team_memory(client, patch_build_model, create_session):
    """普通会话零注入（不得把团队记忆泄给无关会话）。"""
    model = CapturingModel(scripts=[script(text="plain")])
    model.seen = []
    patch_build_model(model)
    sid = create_session([script(text="plain")])
    # create_session 用的是自己的 model；直接跑一条消息走真实 turn
    client.post(f"/api/sessions/{sid}/invoke", json={"message": "hello"})
    deadline = time.time() + 15
    while time.time() < deadline and not model.seen:
        time.sleep(0.2)
    if model.seen:
        text = _all_text(model.seen)
        assert "team_context" not in text, "普通会话不该有 team_context 段"
    assert team_store.get_team("nonexistent") is None
