"""Quick-fix D10：post_reply 返回 False 时广场绝不静默置 done。

缺陷原状：``on_turn_settled`` 忽略 ``post_reply`` 的返回值——回帖写失败
（主会话缺失 / checkpoint 写失败 / 团队成员已删，post_reply 绝不抛、只返回
False）时照样把成员置 done + 「已回帖」+ last_reply_turn 去重 → 投递失败
既不可见又焊死了同一 turn 的重试窗口。

修复后契约：
- 失败 → 不写 last_completed / last_reply_turn（保住收件箱语义与重试窗口）；
- 失败 → sticky ``last_reply_error`` 让聚合层（status.member_status）判
  ``error``（红点 + detail 直读失败文案），成功回帖 / 新投递开始时清除；
- 失败 → 推 ``notice`` 到主会话广场（可见），并发闸照常 drain（turn 已结束）。
"""

from __future__ import annotations

import pytest

from ginno_runtime import server_shared
from ginno_runtime.teams import lifecycle, status as team_status, store as team_store

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    t = team_store.create_team("队", "default", "main-sess")
    team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    return team_store.get_team(t["id"])


@pytest.fixture
def spy(monkeypatch):
    from ginno_runtime.teams import deliver as team_deliver

    calls: dict = {"reply": [], "drain": [], "broadcast": [], "push": []}

    async def _reply(team_id, instance_id, *a, **k):
        calls["reply"].append((team_id, instance_id))
        return calls.pop("reply_result", True)

    async def _drain(team_id):
        calls["drain"].append(team_id)

    async def _bc(team_id, client="main"):
        calls["broadcast"].append(team_id)

    async def _push(session_id, event, data, turn_id=None):
        calls["push"].append((session_id, event, data))

    monkeypatch.setattr(team_deliver, "post_reply", _reply)
    monkeypatch.setattr(team_deliver, "_drain_queue", _drain)
    monkeypatch.setattr(team_deliver, "_broadcast_status", _bc)
    monkeypatch.setattr(server_shared, "_push_session_event", _push)
    return calls


async def test_reply_failure_does_not_set_done(team, spy):
    """post_reply=False：不置 done、不写 last_completed/last_reply_turn，
    sticky last_reply_error + drain 照常 + notice 可见。"""
    spy["reply_result"] = False
    iid = team["members"][0]["instance_id"]
    await lifecycle.on_turn_settled("sess-dev", "turn-1")

    assert spy["reply"] == [(team["id"], iid)]
    m = team_store.get_member(team["id"], iid)
    assert m.get("status") != "done", "回帖失败置 done = 静默丢失（D10）"
    assert not m.get("last_completed"), "失败不产生收件箱 done 语义"
    assert not m.get("last_reply_turn"), "失败不得焊死重试窗口（去重键不写）"
    assert m.get("last_reply_error"), "失败必须留下 sticky 错误信号"
    # turn 本身已结束：并发闸照常放行
    assert team["id"] in spy["drain"]
    # 失败可见：notice 推到主会话广场
    notices = [p for p in spy["push"] if p[1] == "notice"]
    assert notices and notices[0][0] == "main-sess" and notices[0][2].get("message")


async def test_reply_failure_status_aggregates_to_error(team, spy):
    """last_reply_error 经 refresh_team 聚合成 error 态（红点 + detail 直读）。"""
    spy["reply_result"] = False
    iid = team["members"][0]["instance_id"]
    await lifecycle.on_turn_settled("sess-dev", "turn-1")

    m = team_store.get_member(team["id"], iid)
    assert m["status"] == "error"
    assert m["status_detail"] == m["last_reply_error"]


async def test_failed_turn_stays_retryable_then_success_recovers(team, spy):
    """失败不写去重键 → 同一 turn 再次结算会重试回帖；成功后清 sticky 错误。"""
    iid = team["members"][0]["instance_id"]
    spy["reply_result"] = False
    await lifecycle.on_turn_settled("sess-dev", "turn-1")
    # 重试：同一 turn_id 再触发（修复前会被 last_reply_turn 去重吞掉）
    spy["reply_result"] = True
    await lifecycle.on_turn_settled("sess-dev", "turn-1")

    assert len(spy["reply"]) == 2, "失败后同一 turn 必须仍可重试回帖"
    m = team_store.get_member(team["id"], iid)
    assert m["status"] == "done"
    assert m.get("last_reply_turn") == "turn-1"
    assert not m.get("last_reply_error"), "成功回帖清除 sticky 错误"


async def test_new_delivery_clears_sticky_reply_error(team, spy):
    """新投递开始即清 last_reply_error（deliver._run_member_turn 的 patch）。"""
    from ginno_runtime.teams import deliver as team_deliver

    spy["reply_result"] = False
    iid = team["members"][0]["instance_id"]
    await lifecycle.on_turn_settled("sess-dev", "turn-1")
    assert team_store.get_member(team["id"], iid).get("last_reply_error")

    # 只验证清标记的 store patch 路径（不起真 turn）：直接断言 deliver 源头
    # 的投递 patch 含 last_reply_error 置空——真 turn 生命周期另有套件覆盖。
    import inspect

    src = inspect.getsource(team_deliver._run_member_turn)
    assert '"last_reply_error": ""' in src


def test_member_status_reads_last_reply_error(seeded_home):
    """聚合层直测：last_reply_error → error；stored working 也不被 unknown 吞。"""
    member = {"session_id": "sess-x", "status": "working", "last_reply_error": "boom"}
    assert team_status.member_status("default", "sess-x", member) == ("error", "boom")
    member2 = {"session_id": "sess-x", "status": "idle"}
    st, _ = team_status.member_status("default", "sess-x", member2)
    assert st != "error"
