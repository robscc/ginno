"""P1 可靠性修复回归（backlog P1-5 / P1-6 / P1-8 / P1-10）。"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import server_shared
from ginno_runtime.teams import deliver as team_deliver
from ginno_runtime.teams import store as team_store

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    t = team_store.create_team("队", "default", "main-sess")
    team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    team_store.add_member(t["id"], "research", "sess-res", display_name="res")
    return team_store.get_team(t["id"])


# --------------------------------------------------------------------------- #
# P1-5：并发闸预约位
# --------------------------------------------------------------------------- #

def test_reservation_counts_toward_gate(team):
    """已预约但尚未登记 _RUNNING_TURNS 的成员必须计入闸占用。

    缺陷原状：闸检查在 spawn 之前、登记在 task 内部，两个并发 deliver
    可同时通过闸 → 双开 turn / 闸击穿。
    """
    iid = team["members"][0]["instance_id"]
    assert team_deliver._running_members(team) == 0
    team_deliver._reserve(team["id"], iid)
    try:
        assert team_deliver._running_members(team) == 1, "预约位未计入闸（P1-5）"
        # 同一成员重复预约不重复计数
        team_deliver._reserve(team["id"], iid)
        assert team_deliver._running_members(team) == 1
    finally:
        team_deliver._release(team["id"], iid)
    assert team_deliver._running_members(team) == 0


def test_release_is_idempotent_and_clear_queue_drops_reservation(team):
    iid = team["members"][0]["instance_id"]
    team_deliver._reserve(team["id"], iid)
    team_deliver._release(team["id"], iid)
    team_deliver._release(team["id"], iid)   # 再释放不得抛
    assert team_deliver._running_members(team) == 0
    # 解散时清队列也要清预约位，否则额度永久少一个
    team_deliver._reserve(team["id"], iid)
    team_deliver.clear_queue(team["id"])
    assert team_deliver._running_members(team) == 0


def test_live_turn_and_reservation_count_member_once(team, monkeypatch):
    """同一成员既有预约又有活 turn → 仍只算 1（按成员计，不按 turn 计）。"""
    m = team["members"][0]
    monkeypatch.setitem(server_shared._RUNNING_TURNS, m["session_id"], "t1")
    team_deliver._reserve(team["id"], m["instance_id"])
    try:
        assert team_deliver._running_members(team) == 1
    finally:
        team_deliver._release(team["id"], m["instance_id"])


# --------------------------------------------------------------------------- #
# P1-8：领队升降级 / 移除必须同步 leader_instance_id
# --------------------------------------------------------------------------- #

@pytest.mark.api
async def test_demote_leader_clears_pointer(client, patch_build_model):
    """把领队改回 member → leader_instance_id 必须清空。

    缺陷原状：route.py 优先按 leader_instance_id 路由，降级后仍把全部无 @
    消息投给已降级成员，且「已有领队」校验挡住新任命。
    """
    from ginno_runtime.teams.route import route_targets
    from ginno_runtime.testing.fake_model import script

    patch_build_model(script(text="ok"))
    t = client.post("/api/teams", json={"name": "队", "agent_id": "dev"}).json()["team"]
    leader_iid = t["leader_instance_id"]
    assert leader_iid

    r = client.patch(f"/api/teams/{t['id']}/members/{leader_iid}", json={"role": "member"})
    assert r.json()["ok"], r.json()
    after = client.get(f"/api/teams/{t['id']}").json()["team"]
    assert not after.get("leader_instance_id"), "降级后指针未清（P1-8）"
    # 无领队 → 广播全部成员，而不是投给幽灵领队
    targets = route_targets(after, [])
    assert leader_iid in targets and len(targets) >= 1


@pytest.mark.api
async def test_promote_sets_pointer_and_remove_leader_clears(client, patch_build_model):
    from ginno_runtime.testing.fake_model import script

    patch_build_model(script(text="ok"))
    t = client.post("/api/teams", json={"name": "队"}).json()["team"]
    m = client.post(f"/api/teams/{t['id']}/members",
                    json={"agent_id": "dev"}).json()["member"]
    # 升为领队 → 指针指向它
    r = client.patch(f"/api/teams/{t['id']}/members/{m['instance_id']}",
                     json={"role": "leader"})
    assert r.json()["ok"], r.json()
    assert client.get(f"/api/teams/{t['id']}").json()["team"]["leader_instance_id"] == m["instance_id"]
    # 移除领队 → 指针清空（否则路由投给幽灵成员）
    assert client.delete(f"/api/teams/{t['id']}/members/{m['instance_id']}").json()["ok"]
    assert not client.get(f"/api/teams/{t['id']}").json()["team"].get("leader_instance_id")


# --------------------------------------------------------------------------- #
# P1-10：wait 不得死等
# --------------------------------------------------------------------------- #

async def test_wait_aborts_when_members_vanish_midwait(team, monkeypatch):
    """等待过程中成员被移除 → 必须退出，不得永久死循环（默认 timeout=0）。"""
    from ginno_runtime.teams import wait as team_wait

    m = team["members"][0]
    iid = m["instance_id"]
    # 关键前置：成员必须**未安定**（working），否则第一轮就 settled 返回，
    # 根本进不到「等待中成员消失」的那一轮。
    monkeypatch.setitem(server_shared._RUNNING_TURNS, m["session_id"], "t1")

    calls = {"n": 0}
    real = team_store.get_team

    def _flaky(team_id):
        calls["n"] += 1
        t = real(team_id)
        if t is None or calls["n"] <= 1:
            return t
        # 之后成员全部消失（模拟等待中被移除）——_rows() 读的是 get_team
        return {**t, "members": []}

    monkeypatch.setattr(team_store, "get_team", _flaky)
    out = await asyncio.wait_for(
        team_wait.wait_for_members("caller-sess", team["id"], ids=[iid], timeout_s=0),
        timeout=5,
    )
    assert "error" in out.lower() or "不存在" in out, f"应报错退出而非死等：{out}"


async def test_wait_aborts_when_team_disbanded(team, monkeypatch):
    from ginno_runtime.teams import wait as team_wait

    monkeypatch.setattr(team_store, "get_team", lambda tid: None)
    out = await asyncio.wait_for(
        team_wait.wait_for_members("caller-sess", team["id"], timeout_s=0),
        timeout=5,
    )
    assert "error" in out.lower() or "不存在" in out


# --------------------------------------------------------------------------- #
# P1-6：回帖不得因主会话不在内存而静默丢失
# --------------------------------------------------------------------------- #

async def test_post_reply_ensures_main_session(team, monkeypatch):
    """主会话不在 _SESSIONS 时必须兜底加载，而不是 warning+return False。"""
    from ginno_runtime.api import sessions as sessions_api

    ensured: list[str] = []
    real_ensure = sessions_api._ensure_session

    def _spy(sid):
        ensured.append(sid)
        return real_ensure(sid)

    monkeypatch.setattr(sessions_api, "_ensure_session", _spy)
    server_shared._SESSIONS.pop("main-sess", None)

    iid = team["members"][0]["instance_id"]
    # 成员会话也没有产出 → 走「无文本产出」兜底文案，但仍应尝试写主会话
    await team_deliver.post_reply(team["id"], iid, text="成员产出")
    assert "main-sess" in ensured, "主会话未兜底加载 → 回帖静默丢失（P1-6）"
