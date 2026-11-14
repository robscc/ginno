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
