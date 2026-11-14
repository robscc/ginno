"""AgentTeam P0 修复的回归测试（docs/agent-team-backlog.md）。

每个测试对应一个**已确证的生产缺陷**，且刻意避开「用 monkeypatch 造数据源」
的假信心陷阱（P0-4 的教训）——尽量走真实路径。

覆盖：
- P0-2 领队实例会话必须拿到 Lead 工具集（见 test_team_p0.py 的同名回归）
- P0-3 team steer 条目带 origin="team"，段末清场不得丢弃
- P0-5 坏 roster 不得导致其会话被当孤儿删除（不可逆数据丢失）
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths, server_shared
from ginno_runtime.teams import store as team_store

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# P0-3：steer 投递不得静默丢弃
# --------------------------------------------------------------------------- #

def test_team_steer_survives_segment_cleanup(seeded_home):
    """段末清场（steer_keep_channel）必须保留 origin="team" 的未吸收条目。

    缺陷原状：team steer 条目无 origin → 清场只保留 channel/task/worker →
    团队投递被丢弃，而回执已报 delivered（空头支票）。
    """
    sid = "member-sess"
    server_shared.steer_enqueue(sid, {
        "steer_id": "team-ai_1-abc",
        "turn_id": "t1",
        "text": "把 token 刷新改一下",
        "origin": "team",
    })
    # 对照：无 origin 的条目仍应被清掉（原语义不变）
    server_shared.steer_enqueue(sid, {
        "steer_id": "plain-xyz", "turn_id": "t1", "text": "无来源",
    })

    server_shared.steer_keep_channel(sid)

    left = server_shared._STEER_STASH.get(sid) or []
    ids = {e.get("steer_id") for e in left}
    assert "team-ai_1-abc" in ids, "team 投递被段末清场丢弃（P0-3 回归）"
    assert "plain-xyz" not in ids, "无 origin 的条目应照原语义清掉"


def test_deliver_sets_team_origin(seeded_home, monkeypatch):
    """deliver() 走 steer 路径时必须打 origin="team"（否则上一条的保护失效）。"""
    import asyncio

    from ginno_runtime.teams import deliver as team_deliver

    team = team_store.create_team("队", "default", "main-sess")
    m = team_store.add_member(team["id"], "dev", "member-sess")

    # 让成员看起来「在跑」→ 走 steer 分支
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "member-sess", "t1")

    captured: list[dict] = []
    real_enqueue = server_shared.steer_enqueue

    def _spy(session_id, entry):
        captured.append(entry)
        return real_enqueue(session_id, entry)

    monkeypatch.setattr(team_deliver, "steer_enqueue", _spy)
    # 不等 turn 结束（避免测试挂起）
    monkeypatch.setattr(team_deliver, "spawn_bg", lambda coro: coro.close())

    asyncio.run(team_deliver.deliver(team["id"], m["instance_id"], "干活", wait=False))

    assert captured, "应走 steer 分支"
    assert captured[0].get("origin") == "team", "team steer 必须带 origin（P0-3）"


# --------------------------------------------------------------------------- #
# P0-5：坏 roster 不得导致数据丢失
# --------------------------------------------------------------------------- #

def test_unreadable_rosters_detects_corrupt_file(seeded_home):
    team = team_store.create_team("队", "default", "main-sess")
    assert team_store.unreadable_rosters() == []

    # 写坏 roster
    paths.team_path(team["id"]).write_text("{ not json", encoding="utf-8")
    bad = team_store.unreadable_rosters()
    assert team["id"] in bad
    # list_teams 仍静默跳过（读函数契约不变）
    assert team_store.list_teams() == []


def test_unreadable_rosters_detects_structurally_invalid(seeded_home):
    """结构不合法（非 dict / 无 id）同样算读不出——list_teams 也会跳过它。"""
    p = paths.teams_dir() / "t_bad.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"no_id": True}), encoding="utf-8")
    assert "t_bad" in team_store.unreadable_rosters()


@pytest.mark.api
async def test_recover_teams_skips_cleanup_on_corrupt_roster(client, patch_build_model):
    """**数据丢失回归**：roster 损坏时，recover_teams 不得删其会话。

    缺陷原状：referenced 只来自 list_teams()，而后者静默跳过坏文件 →
    坏 roster 团队的全部会话被当孤儿 force 删除（不可逆）。
    """
    from ginno_runtime.api.teams import recover_teams
    from ginno_runtime.testing.fake_model import script

    patch_build_model(script(text="ok"))
    r = client.post("/api/teams", json={"name": "队", "project_slug": "default"}).json()
    assert r["ok"], r
    team = r["team"]
    main_sid = team["main_session_id"]
    mr = client.post(f"/api/teams/{team['id']}/members", json={"agent_id": "dev"}).json()
    assert mr["ok"], mr
    member_sid = mr["member"]["session_id"]

    # 两个会话都真实存在
    ids = {s["id"] for s in client.get("/api/sessions").json()}
    assert main_sid in ids and member_sid in ids

    # 写坏 roster，然后跑启动恢复
    paths.team_path(team["id"]).write_text("{ corrupted", encoding="utf-8")
    await recover_teams()

    ids_after = {s["id"] for s in client.get("/api/sessions").json()}
    assert main_sid in ids_after, "坏 roster 的主会话被误删（P0-5 数据丢失回归）"
    assert member_sid in ids_after, "坏 roster 的成员会话被误删（P0-5 数据丢失回归）"


@pytest.mark.api
async def test_recover_teams_still_cleans_true_orphans(client, patch_build_model):
    """守卫不能把正常清理也关掉：roster 完好时，真孤儿仍须被清掉。"""
    from ginno_runtime.api.sessions import CreateSessionRequest, create_session
    from ginno_runtime.api.teams import recover_teams
    from ginno_runtime.testing.fake_model import script

    patch_build_model(script(text="ok"))
    # 一个不属于任何团队的 team-member 会话 = 真孤儿
    orphan = await create_session(CreateSessionRequest(
        project_slug="default", workspace="/tmp/x", type="team-member",
    ))
    assert orphan["ok"], orphan
    oid = orphan["id"]
    assert oid in {s["id"] for s in client.get("/api/sessions").json()}

    await recover_teams()

    assert oid not in {s["id"] for s in client.get("/api/sessions").json()}, \
        "roster 完好时真孤儿应被清理（守卫不得误伤正常路径）"
