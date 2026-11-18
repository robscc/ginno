"""AgentTeam 投递 e2e：慢启动成员 turn 不得误报 stalled（方案② ack）。

真实链路（HTTP → deliver → spawn → runner 前置 → 登记 → notify → 回执），
仅两处桩：fake LLM（本仓 e2e 惯例）与 `_find_meta` 同步慢 6.2s——复现
生产「登记前的同步 IO 卡住事件循环」（graph 构建/checkpoint 读盘），
即 2026-11-17 干净 main 上复现的误报 stalled 场景。

修复前：deliver 盲轮询 5s 窗口被 6.2s 前置击穿 → receipt=stalled（误报，
turn 随后真的跑起来）。修复后：ack 事件在登记瞬间到达（15s ack 超时内）
→ receipt=delivered。
"""

from __future__ import annotations

import time

import pytest
from conftest import as_fake_model

from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def test_slow_start_delivery_not_falsely_stalled(client, patch_build_model, monkeypatch):
    patch_build_model(as_fake_model(script(text="成员收到，马上处理。")))

    # 慢启动注入：runner 在登记 _RUNNING_TURNS 之前必调 `_find_meta`
    # （同步读盘）。慢 6.2s > 修复前 5s 盲轮询窗口。
    import ginno_runtime.subagent_scheduler as sched

    orig_find = sched._find_meta

    def slow_find(session_id):
        time.sleep(6.2)
        return orig_find(session_id)

    monkeypatch.setattr(sched, "_find_meta", slow_find)

    # 建队（自动建主会话）
    r = client.post("/api/teams", json={"name": "e2e-ack"})
    assert r.status_code == 200, r.text
    team = r.json()
    assert team.get("ok"), team
    team_id = team["team"]["id"]

    # 加成员（server 侧自动建它自己的 session）
    r = client.post(f"/api/teams/{team_id}/members",
                    json={"agent_id": "dev", "display_name": "dev"})
    assert r.status_code == 200, r.text
    member = r.json()
    assert member.get("ok"), member
    iid = member["member"]["instance_id"]

    # 派活——修复前这里 receipt=stalled（误报）
    t0 = time.monotonic()
    r = client.post(f"/api/teams/{team_id}/deliver",
                    json={"instance_id": iid, "text": "报告当前状态"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["receipt"] == "delivered", (
        f"慢启动成员被误报：{receipt}（ack 应在登记瞬间 ~6.2s 处到达）"
    )
    assert receipt["ok"] is True
    # 事件驱动语义：回执应在 ack 到达（~6.2s）后立即返回，而非等满 15s 超时
    assert elapsed < 14.0, f"回执耗时 {elapsed:.1f}s——疑似走了超时兜底而非 ack"

    # turn 真的跑了（ack 不是空头支票）：轮询成员状态离开 working
    # （CLAUDE.md 纪律：落盘竞态用轮询，不裸查一次）
    deadline = time.monotonic() + 30
    status = "working"
    while time.monotonic() < deadline:
        t = client.get(f"/api/teams/{team_id}").json()
        members = (t.get("team") or {}).get("members") or []
        hit = [m for m in members if m.get("instance_id") == iid]
        status = str((hit[0] if hit else {}).get("status") or "?")
        if status != "working":
            break
        time.sleep(0.5)
    assert status != "working", "成员 turn 未在 30s 内离开 working——ack 与实际不符"
