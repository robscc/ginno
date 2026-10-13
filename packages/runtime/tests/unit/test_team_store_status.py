"""AgentTeam 编制存储 + 语义态聚合的单元测试（agent-team-design.md §3）。

纯逻辑层：不建 session、不跑 turn，直接验存储纪律（锁 + 原子落盘）、
成员实例语义（同一 agent 可多实例）、以及 §3.3 的状态判据与 §5.3.1 的上卷。
"""

from __future__ import annotations

import json
import time

import pytest

from ginno_runtime import paths, server_shared
from ginno_runtime.teams import status as team_status
from ginno_runtime.teams import store as team_store

pytestmark = pytest.mark.unit


@pytest.fixture
def team(seeded_home):
    """一个带主会话与两个成员（dev / research）的团队。

    返回**重新读盘**的团队 dict：``create_team`` 的返回值是写入前的快照
    （``members`` 还是空的），``add_member`` 只落盘不改它。测试要断言成员
    就得拿最新的那份——这也是 API 层的真实用法（写完再 get）。
    """
    t = team_store.create_team(
        name="产品迭代小组",
        project_slug="default",
        main_session_id="main-sess",
        workspace="/tmp/ws",
        context_folders=["ginno"],
        primary_folder="ginno",
    )
    assert t is not None
    team_store.add_member(t["id"], "dev", "sess-dev", display_name="dev")
    team_store.add_member(t["id"], "research", "sess-res", display_name="research")
    return team_store.get_team(t["id"])


@pytest.fixture
def pending_resume(monkeypatch):
    """把 session 标为「停在 interrupt 等人回答」。

    ``_PENDING_RESUME`` 是 **set**（不是 dict），所以不能用 monkeypatch.setitem；
    直接 add 并在 teardown 里 discard，避免污染其他测试。
    """
    added: list[str] = []

    def _mark(session_id: str, kind: str = "permission") -> None:
        server_shared._PENDING_RESUME.add(session_id)
        added.append(session_id)
        monkeypatch.setitem(server_shared._PENDING_KIND, session_id, kind)

    yield _mark
    for sid in added:
        server_shared._PENDING_RESUME.discard(sid)


# --------------------------------------------------------------------------- #
# 存储纪律
# --------------------------------------------------------------------------- #

def test_create_team_persists_json_file(team):
    p = paths.team_path(team["id"])
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["name"] == "产品迭代小组"
    assert data["main_session_id"] == "main-sess"
    assert data["shared_workspace"]["primary_folder"] == "ginno"


def test_create_team_rejects_empty_name(seeded_home):
    assert team_store.create_team("", "default", "s1") is None
    assert team_store.create_team("  ", "default", "s1") is None
    # 主会话 id 也是必填（没有它就没有 @ 的落点）
    assert team_store.create_team("x", "default", "") is None


def test_atomic_write_leaves_no_tmp_file(team):
    """tmp+rename 原子落盘：任何时刻目录里不该留下 .tmp 残骸。"""
    team_store.patch_member(team["id"], team["members"][0]["instance_id"], {"status": "working"})
    leftovers = [p.name for p in paths.teams_dir().glob("*.tmp")]
    assert leftovers == []


def test_corrupt_team_file_reads_as_missing(team):
    """坏文件按缺失处理，绝不抛（模块契约）。"""
    paths.team_path(team["id"]).write_text("{ not json", encoding="utf-8")
    assert team_store.get_team(team["id"]) is None
    assert team_store.list_teams() == []


def test_list_teams_filters_by_slug(team):
    other = team_store.create_team("别的组", "other-proj", "main-2")
    assert len(team_store.list_teams()) == 2
    assert [t["name"] for t in team_store.list_teams("default")] == ["产品迭代小组"]
    assert [t["name"] for t in team_store.list_teams("other-proj")] == ["别的组"]
    assert other is not None


# --------------------------------------------------------------------------- #
# 成员实例语义（§3.1：Agent ≠ Session，同一 agent 可多实例）
# --------------------------------------------------------------------------- #

def test_same_agent_can_be_added_twice_as_distinct_instances(team):
    m1 = team_store.add_member(team["id"], "dev", "sess-dev-2", display_name="dev-前端")
    m2 = team_store.add_member(team["id"], "dev", "sess-dev-3", display_name="dev-后端")
    assert m1["instance_id"] != m2["instance_id"]
    assert m1["session_id"] != m2["session_id"]
    devs = [m for m in team_store.list_members(team["id"]) if m["agent_id"] == "dev"]
    assert len(devs) == 3


def test_member_of_agent_resolves_first_instance(team):
    """@dev 只写 agent_id 时解析为最先加入的那个实例（§5.1）。"""
    second = team_store.add_member(team["id"], "dev", "sess-dev-2")
    hit = team_store.member_of_agent(team["id"], "dev")
    assert hit["session_id"] == "sess-dev"
    assert hit["instance_id"] != second["instance_id"]
    assert team_store.member_of_agent(team["id"], "nobody") is None


def test_display_name_defaults_to_agent_id_and_is_renamable(team):
    m = team_store.add_member(team["id"], "writer", "sess-w")
    assert m["display_name"] == "writer"
    renamed = team_store.patch_member(team["id"], m["instance_id"], {"display_name": "文案"})
    assert renamed["display_name"] == "文案"
    # 改名不动寻址键（herdr agent rename 的等价物）
    assert renamed["instance_id"] == m["instance_id"]


def test_member_cap_enforced(team):
    """成员数硬上限（§5.6）：默认 6，已有 2 → 还能加 4 个。"""
    for i in range(4):
        assert team_store.add_member(team["id"], "dev", f"s{i}") is not None
    assert len(team_store.list_members(team["id"])) == team_store.MAX_MEMBERS
    assert team_store.add_member(team["id"], "dev", "overflow") is None


def test_remove_member(team):
    iid = team["members"][0]["instance_id"]
    assert team_store.remove_member(team["id"], iid) is True
    assert team_store.get_member(team["id"], iid) is None
    # 幂等：再删一次返回 False
    assert team_store.remove_member(team["id"], iid) is False


def test_patch_member_never_overwrites_identity(team):
    iid = team["members"][0]["instance_id"]
    out = team_store.patch_member(team["id"], iid, {
        "instance_id": "hacked", "created": 0, "status": "working",
    })
    assert out["instance_id"] == iid
    assert out["created"] != 0
    assert out["status"] == "working"


# --------------------------------------------------------------------------- #
# 反查：session → team / member（投递分支的判据，§5.2 步骤 2）
# --------------------------------------------------------------------------- #

def test_team_of_session_recognizes_main_and_member(team):
    found = team_store.team_of_session("main-sess")
    assert found is not None
    t, member = found
    assert t["id"] == team["id"] and member is None      # 主会话本身

    t2, m2 = team_store.team_of_session("sess-dev")
    assert t2["id"] == team["id"] and m2["agent_id"] == "dev"

    assert team_store.team_of_session("unrelated") is None
    assert team_store.team_of_session("") is None


# --------------------------------------------------------------------------- #
# 语义态判据（§3.3）+ 语义/展示分离（§3.4）
# --------------------------------------------------------------------------- #

def _member(team, agent_id):
    return next(m for m in team_store.list_members(team["id"]) if m["agent_id"] == agent_id)


def test_status_blocked_wins_over_everything(team, pending_resume, monkeypatch):
    """blocked 优先级最高：即使有活 turn 也算 blocked（它在等人）。"""
    pending_resume("sess-dev", "ask_user")
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    status, detail = team_status.member_status("default", "sess-dev", _member(team, "dev"))
    assert status == "blocked"
    assert "回答" in detail


def test_status_working_when_turn_live(team, monkeypatch):
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    status, _ = team_status.member_status("default", "sess-dev", _member(team, "dev"))
    assert status == "working"


def test_status_unknown_not_idle_when_turn_unhealed(team, monkeypatch):
    """重启后 meta 还写着 running 但进程里没有活 turn → unknown，绝不假装 idle。

    这是 §3.3 的核心纪律（herdr：*"not proof of completion"*）。
    """
    monkeypatch.setitem(server_shared._SESSIONS, "sess-dev", {"stop_reason": "running"})
    status, detail = team_status.member_status("default", "sess-dev", _member(team, "dev"))
    assert status == "unknown"
    assert "不可判定" in detail


def test_status_error_from_stop_reason(team, monkeypatch):
    monkeypatch.setitem(server_shared._SESSIONS, "sess-dev", {"stop_reason": "error"})
    status, _ = team_status.member_status("default", "sess-dev", _member(team, "dev"))
    assert status == "error"


def test_status_done_until_seen_then_idle(team, monkeypatch):
    """done = 完成但未读；显式聚焦后转 idle（§3.3 收件箱语义）。"""
    m = _member(team, "dev")
    # last_completed 在 seen 之后 → 未读
    team_store.patch_member(team["id"], m["instance_id"], {"last_completed": time.time() - 100})
    m = _member(team, "dev")
    status, _ = team_status.member_status("default", "sess-dev", m, client="main")
    assert status == "done"

    team_status.mark_seen(team["id"], m["instance_id"], client="main")
    m = _member(team, "dev")
    status, _ = team_status.member_status("default", "sess-dev", m, client="main")
    assert status == "idle"


def test_fresh_member_is_idle_not_done(team):
    """回归：刚加入、从没跑完过 turn 的成员必须读作 idle。

    若误判为 done，每个新成员一上来就顶未读徽标、needs_action 恒真，
    §5.3.1 的上卷信号就废了（判据是 last_completed，不是 last_active）。
    """
    m = team_store.add_member(team["id"], "writer", "sess-w")
    assert m["last_completed"] == 0.0
    status, _ = team_status.member_status("default", "sess-w", m)
    assert status == "idle"
    # 团队级：新成员不该让 needs_action 变真
    team_status.refresh_team(team["id"])
    assert team_status.rollup(team_store.get_team(team["id"]))["needs_action"] is False



def test_seen_is_per_client(team):
    """主窗口看过 ≠ pin 浮窗看过（herdr：each client tracks independently）。"""
    m = _member(team, "dev")
    team_store.patch_member(team["id"], m["instance_id"], {"last_completed": time.time() - 100})
    team_status.mark_seen(team["id"], m["instance_id"], client="main")
    m = _member(team, "dev")
    assert team_status.member_status("default", "sess-dev", m, "main")[0] == "idle"
    assert team_status.member_status("default", "sess-dev", m, "pin")[0] == "done"


def test_status_detail_is_display_only(team, monkeypatch):
    """展示文本变化不得改变语义态（§3.4）。"""
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    m = _member(team, "dev")
    for detail in ("正在跑 pytest", "正在读文件", "", "随便什么"):
        team_store.patch_member(team["id"], m["instance_id"], {"status_detail": detail})
        m = _member(team, "dev")
        assert team_status.member_status("default", "sess-dev", m)[0] == "working"


def test_missing_session_is_unknown(team):
    status, _ = team_status.member_status("default", "", _member(team, "dev"))
    assert status == "unknown"


# --------------------------------------------------------------------------- #
# 上卷（§5.3.1）
# --------------------------------------------------------------------------- #

def test_rollup_priority_blocked_wins(team, pending_resume, monkeypatch):
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    pending_resume("sess-res", "permission")
    team_status.refresh_team(team["id"])
    r = team_status.rollup(team_store.get_team(team["id"]))
    assert r["status"] == "blocked"
    assert r["blocked"] == 1 and r["working"] == 1
    assert r["needs_action"] is True


def test_rollup_working_does_not_need_action(team, monkeypatch):
    """working 止于团队行，不冒泡到全局徽标（§5.3.1 纪律 2）。"""
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    team_status.refresh_team(team["id"])
    r = team_status.rollup(team_store.get_team(team["id"]))
    assert r["status"] == "working"
    assert r["needs_action"] is False


def test_rollup_done_unread_needs_action(team):
    m = _member(team, "dev")
    team_store.patch_member(team["id"], m["instance_id"], {"last_completed": time.time() - 100})
    team_status.refresh_team(team["id"])
    r = team_status.rollup(team_store.get_team(team["id"]))
    assert r["done_unread"] == 1
    assert r["needs_action"] is True


def test_rollup_empty_team_is_idle(team):
    for m in team_store.list_members(team["id"]):
        team_store.remove_member(team["id"], m["instance_id"])
    r = team_status.rollup(team_store.get_team(team["id"]))
    assert r == {"status": "idle", "counts": {}, "total": 0, "working": 0,
                 "blocked": 0, "done_unread": 0, "needs_action": False}


def test_refresh_team_persists_status(team, monkeypatch):
    """状态落盘（§5.8）：重启后仍能显示 done 未读，纯内存态做不到。"""
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    team_status.refresh_team(team["id"])
    on_disk = json.loads(paths.team_path(team["id"]).read_text(encoding="utf-8"))
    dev = next(m for m in on_disk["members"] if m["agent_id"] == "dev")
    assert dev["status"] == "working"


# --------------------------------------------------------------------------- #
# 前端载荷
# --------------------------------------------------------------------------- #

def test_team_payload_shape(team, monkeypatch):
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-dev", "t1")
    payload = team_status.team_payload(team_store.get_team(team["id"]))
    assert payload["id"] == team["id"]
    assert len(payload["member_rows"]) == 2
    row = next(r for r in payload["member_rows"] if r["agent_id"] == "dev")
    assert row["status"] == "working"
    assert row["display_name"] == "dev"
    assert row["session_id"] == "sess-dev"
    assert row["unread"] is False
    assert "elapsed_s" in row
    assert payload["rollup"]["status"] == "working"


# ---- P2 产品重构（2026-10-13）：成员多会话 ----


def test_member_multi_session_lifecycle(isolated_home):
    from ginno_runtime.teams import store as ts

    t = ts.create_team("T", "default", "main_s")
    m = ts.add_member("default" if False else t["id"], "dev", "s1")
    assert m["session_ids"] == ["s1"]
    # 加会话 → 设为活跃，旧会话保留
    m2 = ts.member_add_session(t["id"], m["instance_id"], "s2")
    assert m2["session_ids"] == ["s1", "s2"] and m2["session_id"] == "s2"
    # 幂等去重
    m3 = ts.member_add_session(t["id"], m["instance_id"], "s2")
    assert m3["session_ids"] == ["s1", "s2"]
    # 删非活跃 → 活跃不变
    m4 = ts.member_remove_session(t["id"], m["instance_id"], "s1")
    assert m4["session_ids"] == ["s2"] and m4["session_id"] == "s2"
    # 删活跃 → 切到最新剩余
    ts.member_add_session(t["id"], m["instance_id"], "s3")
    m5 = ts.member_remove_session(t["id"], m["instance_id"], "s2")
    assert m5["session_id"] == "s3"
    # 删最后一条 → None（调用方应顺势 remove_member）
    assert ts.member_remove_session(t["id"], m["instance_id"], "s3") is None
    # team_of_session 认得成员的任意会话（含 P1 存量 session_id 兼容）
    ts.create_team("T2", "default", "main2")
    legacy = ts.add_member(t["id"], "pm", "legacy_s")
    found = ts.team_of_session("legacy_s")
    assert found and found[1]["instance_id"] == legacy["instance_id"]
    found = ts.team_of_session("main2")
    assert found and found[1] is None
