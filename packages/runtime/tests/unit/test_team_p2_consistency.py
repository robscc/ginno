"""P2 一致性/可观测性修复回归（backlog P2-8 / P2-9 / P2-10 / P2-20 / P2-22）。"""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths, server_shared
from ginno_runtime.teams import status as team_status
from ginno_runtime.teams import store as team_store
from ginno_runtime.teams import wait as team_wait

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    t = team_store.create_team("队", "default", "main-sess")
    team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    return team_store.get_team(t["id"])


# --------------------------------------------------------------------------- #
# P2-9：并发闸 settings 化
# --------------------------------------------------------------------------- #

def test_concurrency_default_is_three(seeded_home):
    assert team_store.max_concurrent_running() == 3


def test_concurrency_reads_settings(seeded_home):
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"team": {"max_concurrent": 5}}), encoding="utf-8")
    assert team_store.max_concurrent_running() == 5


def test_concurrency_env_overrides_settings(seeded_home, monkeypatch):
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"team": {"max_concurrent": 5}}), encoding="utf-8")
    monkeypatch.setenv("GINNO_TEAM_MAX_CONCURRENT", "2")
    assert team_store.max_concurrent_running() == 2


def test_concurrency_clamped_and_bad_value_falls_back(seeded_home, monkeypatch):
    monkeypatch.setenv("GINNO_TEAM_MAX_CONCURRENT", "999")
    assert team_store.max_concurrent_running() == team_store.MAX_CONCURRENT_RUNNING_LIMIT
    monkeypatch.setenv("GINNO_TEAM_MAX_CONCURRENT", "0")
    assert team_store.max_concurrent_running() == team_store.MIN_CONCURRENT_RUNNING
    monkeypatch.setenv("GINNO_TEAM_MAX_CONCURRENT", "not-a-number")
    assert team_store.max_concurrent_running() == 3


# --------------------------------------------------------------------------- #
# P2-8：归档团队不点亮徽标
# --------------------------------------------------------------------------- #

@pytest.mark.api
async def test_archived_team_excluded_from_badges(client, patch_build_model):
    from ginno_runtime.testing.fake_model import script

    patch_build_model(script(text="ok"))
    t = client.post("/api/teams", json={"name": "队", "agent_id": "dev"}).json()["team"]
    iid = t["leader_instance_id"]
    # 造一个 blocked：领队停在 interrupt
    m = team_store.get_member(t["id"], iid)
    server_shared._PENDING_RESUME.add(m["session_id"])
    try:
        team_status.refresh_team(t["id"])
        assert client.get("/api/teams/badge").json()["blocked"] >= 1
        assert client.get("/api/badge").json()["blocked"] >= 1
        # 归档 → 两个端点都不再计数
        assert client.patch(f"/api/teams/{t['id']}", json={"archived": True}).json()["ok"]
        assert client.get("/api/teams/badge").json()["blocked"] == 0
        assert client.get("/api/badge").json()["blocked"] == 0
    finally:
        server_shared._PENDING_RESUME.discard(m["session_id"])


# --------------------------------------------------------------------------- #
# P2-10：working 展示文本有数据源
# --------------------------------------------------------------------------- #

def test_working_detail_uses_last_tool(team, monkeypatch):
    """last_tool 有值时展示具体工具名（此前全仓无写入点 → 永远「正在运行」）。"""
    m = team["members"][0]
    monkeypatch.setitem(server_shared._RUNNING_TURNS, m["session_id"], "t1")
    monkeypatch.setitem(server_shared._SESSIONS, m["session_id"], {"last_tool": "bash"})
    st, detail = team_status.member_status("default", m["session_id"], m)
    assert st == "working"
    assert "bash" in detail


def test_working_detail_falls_back_without_last_tool(team, monkeypatch):
    m = team["members"][0]
    monkeypatch.setitem(server_shared._RUNNING_TURNS, m["session_id"], "t1")
    st, detail = team_status.member_status("default", m["session_id"], m)
    assert st == "working"
    assert detail  # 通用文案，不得为空


# --------------------------------------------------------------------------- #
# P2-20 / P2-22：wait 语义
# --------------------------------------------------------------------------- #

def test_idle_satisfies_done_target():
    """用户 seen 后 done→idle，until=["done"] 仍须收敛（否则一查看就卡死）。"""
    assert team_wait._settled("done", ["done"]) is True
    assert team_wait._settled("idle", ["done"]) is True
    # 但 idle 不满足 blocked 目标
    assert team_wait._settled("idle", ["blocked"]) is False
    assert team_wait._settled("working", ["done"]) is False


def test_default_settled_set_unchanged():
    for s in team_wait.SETTLED_STATUSES:
        assert team_wait._settled(s, None) is True
    assert team_wait._settled("working", None) is False
    assert team_wait._settled("waiting", None) is False


async def test_invalid_until_fails_fast(team):
    """until 传错状态名必须立刻报错，不得永久死等（默认 timeout=0）。"""
    out = await team_wait.wait_for_members(
        "caller", team["id"], until=["finished"], timeout_s=0
    )
    assert "error" in out.lower()
    assert "finished" in out


# --------------------------------------------------------------------------- #
# P2-26：编制写入边角
# --------------------------------------------------------------------------- #

def test_remove_last_session_actually_persists(team):
    """空 ids 分支必须真写盘（此前注释谎称「已写盘」但没调 _write）。"""
    m = team["members"][0]
    iid = m["instance_id"]
    team_store.patch_member(team["id"], iid, {"session_ids": ["sess-dev"]})
    out = team_store.member_remove_session(team["id"], iid, "sess-dev")
    assert out is None  # 一条不剩 → 调用方收尾 remove_member
    # 关键：编制里那条 session 必须真的没了（重读磁盘，不是内存快照）
    fresh = team_store.get_member(team["id"], iid)
    assert fresh is not None
    assert "sess-dev" not in (fresh.get("session_ids") or []), \
        "空 ids 分支未写盘，编制仍留已删 session（P2-26）"
    assert not fresh.get("session_id")


def test_update_team_rejects_unknown_leader_instance(team):
    """leader_instance_id 只接受编制里真实存在的实例（否则路由投给幽灵）。"""
    good = team["members"][0]["instance_id"]
    assert team_store.update_team(team["id"], {"leader_instance_id": good})
    assert team_store.get_team(team["id"])["leader_instance_id"] == good
    # 非法值被忽略，保留原值
    team_store.update_team(team["id"], {"leader_instance_id": "ai_ghost"})
    assert team_store.get_team(team["id"])["leader_instance_id"] == good
    # 空串合法（清空，回落 role 扫描）
    team_store.update_team(team["id"], {"leader_instance_id": ""})
    assert not team_store.get_team(team["id"])["leader_instance_id"]


def test_update_team_still_patches_normal_fields(team):
    """校验不得误伤普通字段。"""
    team_store.update_team(team["id"], {"name": "改名", "archived": True})
    t = team_store.get_team(team["id"])
    assert t["name"] == "改名" and t["archived"] is True


# --------------------------------------------------------------------------- #
# P2-21：多会话成员回帖必须读「真正跑完 turn 的那条会话」
# --------------------------------------------------------------------------- #

async def test_post_reply_reads_source_session_not_active(team, monkeypatch):
    """turn 期间切换活跃会话后，回帖仍须读原会话的产出。

    缺陷原状：post_reply 一律读 member.session_id（当前活跃），多会话成员
    切换后回帖张冠李戴。
    """
    from ginno_runtime.teams import deliver as team_deliver

    m = team["members"][0]
    iid = m["instance_id"]
    # 活跃会话切到 sess-other，但 turn 实际跑在 sess-dev
    team_store.patch_member(team["id"], iid, {
        "session_id": "sess-other", "session_ids": ["sess-dev", "sess-other"],
    })

    read: list[str] = []

    async def _fake_final(slug, sid, team_id=""):
        read.append(sid)
        return f"产出来自 {sid}"

    monkeypatch.setattr(team_deliver, "_final_text", _fake_final)
    # 主会话写盘会失败（无 graph），但取文本这一步的 sid 已被记录
    await team_deliver.post_reply(team["id"], iid, source_session_id="sess-dev")
    assert read == ["sess-dev"], f"回帖读错会话（P2-21）：{read}"


async def test_post_reply_falls_back_to_active_without_source(team, monkeypatch):
    """不传 source 时回落活跃会话（兼容旧调用方）。"""
    from ginno_runtime.teams import deliver as team_deliver

    m = team["members"][0]
    read: list[str] = []

    async def _fake_final(slug, sid, team_id=""):
        read.append(sid)
        return "x"

    monkeypatch.setattr(team_deliver, "_final_text", _fake_final)
    await team_deliver.post_reply(team["id"], m["instance_id"])
    assert read == [m["session_id"]]


# --------------------------------------------------------------------------- #
# 回帖不截断（2026-11-15 产品决定）
# --------------------------------------------------------------------------- #

def test_long_reply_is_not_truncated():
    """回帖全文保留——此前 REPLY_MAX_CHARS=1200 会截断，评审类产出看不到结论。"""
    from ginno_runtime.teams import deliver as team_deliver

    long_text = "评审结论。" * 2000   # 远超旧上限 1200
    out, cut = team_deliver._truncate(long_text)
    assert cut is False, "回帖不应再被标记截断"
    assert out == long_text.strip(), "回帖正文被截断了"
    assert "…" not in out[-5:], "不应追加省略号"


def test_short_reply_unchanged():
    from ginno_runtime.teams import deliver as team_deliver

    out, cut = team_deliver._truncate("  短回帖  ")
    assert out == "短回帖" and cut is False
