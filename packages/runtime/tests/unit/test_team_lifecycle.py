"""turn 生命周期钩子回归（backlog P0-7 / P1-2 / P1-3 / P1-4）。

钩子把「回帖 + 状态回流 + 排队 drain」从 deliver 自有路径搬到 engine/resume
的**真实终态点**，成为唯一触发源。本文件验证四条纪律：

- P0-7 parked（撞权限/ask_user 卡）时**不得回帖**（半截话），终态才回
- P1-4 同一 turn 多次触发**只回一帖**（turn_id 去重）
- P1-2 非 deliver 起的 turn（用户在工作台自起）也触发状态回流
- P1-3 同上路径也 drain 排队
- 钩子对非团队成员会话零副作用，且绝不带崩 turn
"""

from __future__ import annotations

import pytest

from ginno_runtime import server_shared
from ginno_runtime.teams import lifecycle, store as team_store

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    t = team_store.create_team("队", "default", "main-sess")
    team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    return team_store.get_team(t["id"])


@pytest.fixture
def spy(monkeypatch):
    """记录 post_reply / drain / broadcast 的调用（验证钩子行为，不造数据源）。"""
    from ginno_runtime.teams import deliver as team_deliver

    calls = {"reply": [], "drain": [], "broadcast": []}

    async def _reply(team_id, instance_id, *a, **k):
        calls["reply"].append((team_id, instance_id))
        return True

    async def _drain(team_id):
        calls["drain"].append(team_id)

    async def _bc(team_id, client="main"):
        calls["broadcast"].append(team_id)

    monkeypatch.setattr(team_deliver, "post_reply", _reply)
    monkeypatch.setattr(team_deliver, "_drain_queue", _drain)
    monkeypatch.setattr(team_deliver, "_broadcast_status", _bc)
    return calls


# --------------------------------------------------------------------------- #
# P0-7：parked 不得回帖
# --------------------------------------------------------------------------- #

async def test_parked_turn_does_not_reply(team, spy, monkeypatch):
    """成员停在 interrupt 时，钩子只冒泡 blocked，**绝不回帖**。

    缺陷原状：_run_member_turn 把 _run_managed_turn 的返回当 turn 结束，
    parked 也立即回帖 → 主会话收到卡住前的半截话并被置 done。
    """
    iid = team["members"][0]["instance_id"]
    server_shared._PENDING_RESUME.add("sess-dev")
    try:
        await lifecycle.on_turn_settled("sess-dev", "turn-1")
        assert spy["reply"] == [], "parked 时回帖 = 骗主会话（P0-7）"
        # 但状态要冒泡（blocked 实时可见，P1-2）
        assert spy["broadcast"], "parked 应触发状态广播"
        m = team_store.get_member(team["id"], iid)
        assert m["status"] == "blocked"
    finally:
        server_shared._PENDING_RESUME.discard("sess-dev")


async def test_settled_turn_replies_once(team, spy):
    """真正终态 → 回帖 + 置 done + drain。"""
    iid = team["members"][0]["instance_id"]
    await lifecycle.on_turn_settled("sess-dev", "turn-1")
    assert spy["reply"] == [(team["id"], iid)]
    assert spy["drain"] == [team["id"]], "终态须放行排队（P1-3）"
    m = team_store.get_member(team["id"], iid)
    assert m["status"] == "done"
    assert m["last_reply_turn"] == "turn-1"
    assert m["last_completed"] > 0


# --------------------------------------------------------------------------- #
# P1-4：同一 turn 只回一帖
# --------------------------------------------------------------------------- #

async def test_duplicate_settle_same_turn_replies_once(team, spy):
    """engine 与 resume 对同一 turn 各触发一次 → 只回一帖。

    缺陷原状：每条 steer 投递各 spawn 一个 waiter，N 次 steer = N 条重复回帖。
    """
    await lifecycle.on_turn_settled("sess-dev", "turn-A")
    await lifecycle.on_turn_settled("sess-dev", "turn-A")
    await lifecycle.on_turn_settled("sess-dev", "turn-A")
    assert len(spy["reply"]) == 1, "同一 turn 重复回帖（P1-4）"


async def test_different_turns_each_reply(team, spy):
    """不同 turn 各自回帖（去重不能误伤正常的多轮）。"""
    await lifecycle.on_turn_settled("sess-dev", "turn-A")
    await lifecycle.on_turn_settled("sess-dev", "turn-B")
    assert len(spy["reply"]) == 2


async def test_empty_turn_id_not_deduped(team, spy):
    """turn_id 为空时不去重——宁可多回，不可漏回（漏回是 P0-7 的事故形状）。"""
    await lifecycle.on_turn_settled("sess-dev", "")
    await lifecycle.on_turn_settled("sess-dev", "")
    assert len(spy["reply"]) == 2


# --------------------------------------------------------------------------- #
# P1-2 / P1-3：非 deliver 起的 turn 也触发（用户在工作台自起）
# --------------------------------------------------------------------------- #

async def test_hook_fires_for_user_started_turn(team, spy):
    """钩子按 session 判型，不关心 turn 是谁起的。

    缺陷原状：refresh/drain 只挂 deliver 自有路径，用户在工作台手动开跑的
    turn 结束不 drain，但回执已报 delivered/channel=queue → 排队永久搁浅。
    """
    # 不经 deliver，直接调钩子（模拟 engine 终态点触发）
    await lifecycle.on_turn_settled("sess-dev", "user-turn")
    assert spy["reply"], "用户自起 turn 结束也须回帖"
    assert spy["drain"], "用户自起 turn 结束也须 drain 排队（P1-3）"


# --------------------------------------------------------------------------- #
# 零副作用 + 绝不带崩 turn
# --------------------------------------------------------------------------- #

async def test_non_member_session_is_noop(seeded_home, spy):
    """普通会话 / 主会话 / 未知 session → 完全 no-op（不得影响 subagent 等）。"""
    t = team_store.create_team("队", "default", "main-sess")
    await lifecycle.on_turn_settled("main-sess", "t")   # 主会话
    await lifecycle.on_turn_settled("random-sess", "t")  # 无关会话
    await lifecycle.on_turn_parked("main-sess")
    assert spy["reply"] == [] and spy["drain"] == [] and spy["broadcast"] == []
    assert t is not None


async def test_hook_never_raises(team, monkeypatch):
    """钩子内部异常必须被吞掉——绝不带崩成员的 turn。"""
    from ginno_runtime.teams import deliver as team_deliver

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(team_deliver, "post_reply", _boom)
    # 不得抛出
    await lifecycle.on_turn_settled("sess-dev", "turn-X")
    await lifecycle.on_turn_parked("sess-dev")


async def test_locate_rejects_main_session(team):
    """_locate 只认成员实例，主会话命中返回 None。"""
    assert lifecycle._locate("main-sess") is None
    found = lifecycle._locate("sess-dev")
    assert found is not None
    assert found[2]["agent_id"] == "dev"
