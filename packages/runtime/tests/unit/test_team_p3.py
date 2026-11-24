"""AgentTeam P3 验收（agent-team-design.md §6 P3 + §5.4 记忆 + §5.6 并发闸，
2026-10-14 实施）：

* 记忆分层：TEAM.md / 实例 notes.md 读写 + turn_context 注入边界
* ``wait_for_members``（§5.3.2）：默认安定即回 / until 过滤 / 超时 partial /
  协作式打断
* 并发闸（§5.6）：满 3 个在跑 → 排队（delivered + channel=queue），腾额度
  自动 drain；blocked 原子拒绝优先于排队
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths, server_shared
from ginno_runtime.teams import deliver as team_deliver
from ginno_runtime.teams import memory as team_memory
from ginno_runtime.teams import store as team_store
from ginno_runtime.teams import wait as team_wait

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    """主会话 + dev/research 两个成员（member_status 层面 idle）。"""
    t = team_store.create_team(
        name="产品迭代小组", project_slug="default",
        main_session_id="main-sess",
    )
    assert t is not None
    team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    team_store.add_member(t["id"], "research", "sess-res", display_name="research")
    return team_store.get_team(t["id"])


@pytest.fixture
def live_turns(monkeypatch):
    """登记进 _RUNNING_TURNS 的 session 在 teardown 时全部释放。"""
    added: list[str] = []

    def _mark(session_id: str, turn_id: str = "t-x") -> None:
        server_shared._RUNNING_TURNS[session_id] = turn_id
        added.append(session_id)

    yield _mark
    for sid in added:
        server_shared._RUNNING_TURNS.pop(sid, None)
    team_deliver._QUEUES.clear()


# --------------------------------------------------------------------------- #
# 记忆分层（§5.4）
# --------------------------------------------------------------------------- #
def test_team_md_and_notes_roundtrip(team):
    team_memory.set_team_md(team["id"], "# 约定\n禁改 public API")
    assert team_memory.team_md(team["id"]) == "# 约定\n禁改 public API"
    iid = team["members"][0]["instance_id"]
    team_memory.set_member_notes(team["id"], iid, "dev 专用：先跑测试")
    assert team_memory.member_notes(team["id"], iid) == "dev 专用：先跑测试"
    # 空写 = 清空（空记忆不注入）
    team_memory.set_member_notes(team["id"], iid, "  ")
    assert team_memory.member_notes(team["id"], iid) == ""
    assert not list(paths.teams_dir().glob("**/*.tmp"))


def test_turn_context_layers(team):
    """TEAM.md 全员 + 主会话可见；实例 notes 只进该成员；普通会话零注入。"""
    team_memory.set_team_md(team["id"], "TEAM 约定内容")
    iid = team["members"][0]["instance_id"]
    team_memory.set_member_notes(team["id"], iid, "dev 专属备注")

    dev_ctx = team_memory.turn_context("sess-dev")
    assert "TEAM 约定内容" in dev_ctx and "dev 专属备注" in dev_ctx
    res_ctx = team_memory.turn_context("sess-res")
    assert "TEAM 约定内容" in res_ctx and "dev 专属备注" not in res_ctx
    main_ctx = team_memory.turn_context("main-sess")
    assert "TEAM 约定内容" in main_ctx and "备注" not in main_ctx
    # 非团队会话（含成员看不到的随机 id）零注入
    assert team_memory.turn_context("sess-unknown") == ""


def test_turn_context_empty_md_injects_nothing(team):
    """TEAM.md 与 notes 都空 → 不注入**记忆内容**。

    但成员仍恒拿到「投递≠批准」权限纪律条款（P2-11，设计 §5.7）——那是安全
    纪律不是记忆内容，不能因为用户没写 TEAM.md 就消失。
    """
    assert team_memory.turn_context("main-sess") == ""
    ctx = team_memory.turn_context("sess-dev")
    assert "TEAM.md" not in ctx and "notes" not in ctx.lower().replace("not approval", "")
    low = ctx.lower()
    assert "not approval" in low or "不等于批准" in ctx


# --------------------------------------------------------------------------- #
# wait 原语（§5.3.2）
# --------------------------------------------------------------------------- #
async def test_wait_returns_immediately_when_settled(team):
    out = await team_wait.wait_for_members("main-sess", team["id"])
    data = json.loads(out)
    assert data["partial"] is False
    assert {m["status"] for m in data["members"]} == {"idle"}


async def test_wait_blocks_on_working_then_times_out(team, live_turns):
    live_turns("sess-dev")
    out = await team_wait.wait_for_members(
        "main-sess", team["id"], ids=[_iid(team, "dev")], timeout_s=1
    )
    assert "(partial)" in out
    # Parse the payload rather than matching separators: jsonx/orjson emits the
    # compact form, stdlib json the spaced one.
    assert json.loads(out[out.index("{") :])["members"][0]["status"] == "working"


async def test_wait_until_specific_status(team, live_turns):
    """until=["working"] —— 等「进入 working」而不是等安定。"""
    live_turns("sess-dev")
    out = await team_wait.wait_for_members(
        "main-sess", team["id"], ids=[_iid(team, "dev")], until=["working"]
    )
    assert json.loads(out)["partial"] is False


async def test_wait_interrupted_by_cooperative_stop(team, live_turns, monkeypatch):
    live_turns("sess-dev")
    evt = asyncio.Event()
    evt.set()
    monkeypatch.setitem(server_shared._TURN_STOP, "main-sess", evt)
    out = await team_wait.wait_for_members("main-sess", team["id"], timeout_s=0)
    # timeout_s=0 → 不设 deadline，靠 stop 事件退出
    assert out.startswith("[stopped]")


async def test_wait_unknown_ids_error(team):
    out = await team_wait.wait_for_members(
        "main-sess", team["id"], ids=["ai_nope"], timeout_s=0
    )
    assert out.startswith("[error]")


# --------------------------------------------------------------------------- #
# 并发闸（§5.6）
# --------------------------------------------------------------------------- #
async def test_deliver_queues_when_concurrency_full(team, live_turns, monkeypatch):
    """三个成员在跑（上限 3）→ 第四个（空闲）投递排队：delivered + channel=queue。"""
    team_store.add_member(team["id"], "writer", "sess-wri", display_name="writer")
    team_store.add_member(team["id"], "ops", "sess-ops", display_name="ops")
    team = team_store.get_team(team["id"])  # 快照不含新成员，重读
    for sid in ("sess-dev", "sess-res", "sess-wri"):
        live_turns(sid)

    started: list[tuple] = []

    async def fake_turn(team_id, instance_id, text):
        sid = team_store.get_member(team_id, instance_id)["session_id"]
        server_shared._RUNNING_TURNS[sid] = "t-drain"
        started.append((instance_id, text))
        server_shared.notify_turn_started(sid)
        server_shared._RUNNING_TURNS.pop(sid, None)
        team_deliver._release(team_id, instance_id)

    monkeypatch.setattr(team_deliver, "_run_member_turn", fake_turn)
    monkeypatch.setattr(team_deliver, "_ACK_TIMEOUT_S", 0.1)

    # 单测没有真实 session：_ensure_session 给个假壳，让 deliver 走到 spawn。
    from ginno_runtime.api import sessions as sessions_api

    monkeypatch.setattr(
        sessions_api, "_ensure_session",
        lambda sid: {"graph": object(), "agent_id": "dev", "project_slug": "default"},
    )

    res = await team_deliver.deliver(team["id"], _iid(team, "ops"), "盘点一下")
    assert res["receipt"] == "delivered" and res["channel"] == "queue"
    assert res["queued"] == 1
    assert started == []  # 没有立刻开跑
    member = team_store.get_member(team["id"], _iid(team, "ops"))
    assert "Queued" in member["status_detail"]

    drained = []
    deliver = team_deliver.deliver

    async def record_delivery(*args, **kwargs):
        receipt = await deliver(*args, **kwargs)
        drained.append((receipt["receipt"], receipt["channel"], receipt["instance_id"]))
        return receipt

    monkeypatch.setattr(team_deliver, "deliver", record_delivery)
    # 腾出一个额度 → drain 自动放行排队投递
    server_shared._RUNNING_TURNS.pop("sess-dev", None)
    await team_deliver._drain_queue(team["id"])
    assert started == [(_iid(team, "ops"), "盘点一下")]
    assert drained == [("delivered", "turn", _iid(team, "ops"))]
    assert team_deliver._QUEUES.get(team["id"]) in (None, [])


async def test_deliver_blocked_refusal_precedes_queue(team, live_turns, monkeypatch):
    """并发满时给 blocked 成员投递 → 仍是原子拒绝（纪律 1 优先于排队）。"""
    for sid in ("sess-dev", "sess-res", "main-sess"):
        live_turns(sid)
    server_shared._PENDING_RESUME.add("sess-dev")
    try:
        res = await team_deliver.deliver(team["id"], _iid(team, "dev"), "喂")
        assert res["receipt"] == "failed" and res["reason"] == "agent_blocked"
        assert team_deliver._QUEUES.get(team["id"]) in (None, [])
    finally:
        server_shared._PENDING_RESUME.discard("sess-dev")


def _iid(team: dict, agent_id: str) -> str:
    return str(next(m for m in team["members"] if m["agent_id"] == agent_id)["instance_id"])


# --------------------------------------------------------------------------- #
# P2-11：「投递≠批准」必须进成员系统提示 + 新 turn 路径带溯源信封
# --------------------------------------------------------------------------- #

def test_delivery_is_not_approval_in_member_prompt(seeded_home):
    """设计 §5.7：这条要写进成员的系统提示，防模型把 @ 当免检通行证。

    此前全 runtime grep 相关文案零命中——要求未实现。
    """
    from ginno_runtime.teams import memory as team_memory
    from ginno_runtime.teams import store as team_store

    t = team_store.create_team("队", "default", "main-sess")
    m = team_store.add_member(t["id"], "dev", "sess-dev")
    ctx = team_memory.turn_context("sess-dev")
    low = ctx.lower()
    assert "not approval" in low or "不等于批准" in ctx, f"缺「投递≠批准」条款：{ctx}"
    assert "permission" in low or "permission" in ctx
    assert m["instance_id"]


def test_plaza_main_session_gets_no_member_clause(seeded_home):
    """主会话（广场）不是成员，不该拿到成员专属的权限纪律条款。"""
    from ginno_runtime.teams import memory as team_memory
    from ginno_runtime.teams import store as team_store

    t = team_store.create_team("队", "default", "main-sess")
    ctx = team_memory.turn_context("main-sess")
    assert "not approval" not in ctx.lower()
    assert "不等于批准" not in ctx
    assert t is not None


def test_new_turn_path_carries_provenance_envelope(seeded_home, monkeypatch):
    """新 turn 路径必须与 steer 路径同口径带 ginno_team_delivery 溯源键。

    此前 _run_member_turn 传 extra_kwargs=None → 成员无从判断消息来自哪个
    团队/实例（steer 路径一直有）。
    """
    import asyncio

    from ginno_runtime.teams import deliver as team_deliver
    from ginno_runtime.teams import store as team_store

    t = team_store.create_team("队", "default", "main-sess")
    m = team_store.add_member(t["id"], "dev", "sess-dev")

    captured: dict = {}

    async def _fake_run(sid, text, extra_kwargs, stop_evt=None):
        captured["extra"] = extra_kwargs

    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_run_managed_turn", _fake_run)
    asyncio.run(team_deliver._run_member_turn(t["id"], m["instance_id"], "干活"))
    env = (captured.get("extra") or {}).get("ginno_team_delivery")
    assert env, f"新 turn 路径缺溯源信封（P2-11）：{captured}"
    assert env["team_id"] == t["id"]
    assert env["instance_id"] == m["instance_id"]
