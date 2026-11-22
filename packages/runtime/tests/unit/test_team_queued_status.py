# 并发闸排队态呈现（2026-11-22 巡检 P1）：排队成员语义态 idle，但
# `_idle_detail` 刻意无视 stored status_detail（probe 2026-10-24 防御），
# 投递写入的「排队中」被覆盖成「空闲」→ 面板诱导重复投递。修复：
# deliver.queued_position 只读位次 + member_row idle 分支覆盖 detail。

from __future__ import annotations

import pytest

from ginno_runtime.teams import deliver as team_deliver
from ginno_runtime.teams import status as team_status
from ginno_runtime.teams import store as team_store

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    t = team_store.create_team(
        name="排队呈现小组",
        project_slug="default",
        main_session_id="main-sess-q",
        workspace="/tmp/ws",
        context_folders=["ginno"],
        primary_folder="ginno",
    )
    team_store.add_member(t["id"], "dev", "sess-dev-q", display_name="dev")
    return team_store.get_team(t["id"])


@pytest.fixture
def clean_queue():
    """清空 deliver 私有 _QUEUES，防测试间串染。"""
    team_deliver._QUEUES.clear()
    yield
    team_deliver._QUEUES.clear()


def _member(team):
    return (team.get("members") or [])[0]


def test_queued_member_shows_queue_detail(team, clean_queue):
    m = _member(team)
    team_deliver._QUEUES[team["id"]] = [{"instance_id": m["instance_id"], "text": "x"}]
    row = team_status.member_row(team, m)
    assert row["status"] == "idle"  # 语义态不变，前端无需新枚举
    d = row["status_detail"]
    assert ("排队中" in d and "第 1 位" in d) or (
        "Queued" in d and "#1" in d
    )


def test_second_in_queue_reports_position(team, clean_queue):
    m = _member(team)
    team_deliver._QUEUES[team["id"]] = [
        {"instance_id": "someone-else", "text": "a"},
        {"instance_id": m["instance_id"], "text": "b"},
    ]
    row = team_status.member_row(team, m)
    d = row["status_detail"]
    assert ("第 2 位" in d) or ("#2" in d)
    assert team_deliver.queued_position(team["id"], m["instance_id"]) == 2


def test_plain_idle_stays_idle(team, clean_queue):
    # probe 2026-10-24 防御不被破坏：无队列时 stored detail 仍被无视
    m = _member(team)
    team_store.patch_member(team["id"], m["instance_id"], {
        "status_detail": "正在处理投递",
    })
    fresh = team_store.get_team(team["id"])
    row = team_status.member_row(fresh, _member(fresh))
    assert row["status_detail"] in ("空闲", "Idle")


def test_drain_returns_to_idle(team, clean_queue):
    m = _member(team)
    team_deliver._QUEUES[team["id"]] = [{"instance_id": m["instance_id"], "text": "x"}]
    team_deliver._QUEUES.pop(team["id"], None)  # drain 语义
    row = team_status.member_row(team, m)
    assert row["status_detail"] in ("空闲", "Idle")


def test_queued_position_zero_for_unknown(team, clean_queue):
    assert team_deliver.queued_position("no-such-team", "no-such-member") == 0
