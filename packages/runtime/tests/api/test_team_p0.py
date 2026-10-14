"""AgentTeam P0 验收（agent-team-design.md §6 P0）：

    建团队 → 主会话 @dev → dev 在自己 session 跑 → 回帖出现在主会话
    → 面板状态 working → done → 点进工作台后转 idle
    → research blocked 时 @ 它被原子拒绝

用 ScriptedChatModel 驱动真实 LangGraph + 真实 FileCheckpointer，零网络。
成员的 turn 走 subagent 调度器的 ``_run_managed_turn``（复用其全部脏活）。
"""

from __future__ import annotations

import json
import time as _time

import pytest

from ginno_runtime import server_shared
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


def _model(*texts):
    """一个真正可跑的 ScriptedChatModel（每个 text 一轮 AIMessage）。

    注意：``patch_build_model(script(...))`` 传的是**裸 AIMessage**——建 session
    够用（不 invoke 模型），但一旦真跑 turn 就会 ``'AIMessage' has no attribute
    'ainvoke'``。投递测试要跑成员的 turn，必须包成 ScriptedChatModel。
    """
    return ScriptedChatModel(scripts=[script(text=t) for t in texts])



def _bypass_on():
    from ginno_runtime import paths

    sp = paths.settings_path()
    s = json.loads(sp.read_text() or "{}") if sp.exists() else {}
    s["bypass_permissions"] = True
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(s))


def _create_team(client, name="产品迭代小组", slug="default"):
    r = client.post("/api/teams", json={"name": name, "project_slug": slug})
    data = r.json()
    assert r.status_code == 200 and data.get("ok"), data
    return data["team"]


def _add_member(client, team_id, agent_id, display_name="", role="member"):
    r = client.post(
        f"/api/teams/{team_id}/members",
        json={"agent_id": agent_id, "display_name": display_name, "role": role},
    )
    data = r.json()
    assert r.status_code == 200 and data.get("ok"), data
    return data["member"]


def _get_team(client, team_id, client_key="main"):
    r = client.get(f"/api/teams/{team_id}?client={client_key}")
    data = r.json()
    assert data.get("ok"), data
    return data["team"]


def _member_row(team, agent_id):
    return next(m for m in team["member_rows"] if m["agent_id"] == agent_id)


# --------------------------------------------------------------------------- #
# 团队 / 成员 CRUD
# --------------------------------------------------------------------------- #

def test_create_team_makes_main_session(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    assert team["name"] == "产品迭代小组"
    assert team["main_session_id"]
    # 主会话是真实 session（type=team），侧栏能归类
    sessions = client.get("/api/sessions").json()
    main = next(s for s in sessions if s["id"] == team["main_session_id"])
    assert main["type"] == "team"


def test_add_member_creates_independent_session(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev", display_name="dev")
    assert m["agent_id"] == "dev"
    assert m["session_id"] and m["session_id"] != team["main_session_id"]
    # 成员 session 的 parent 指向主会话（复用级联删除 + 侧栏树，§3.2）
    sessions = client.get("/api/sessions").json()
    ms = next(s for s in sessions if s["id"] == m["session_id"])
    assert ms["type"] == "team-member"
    assert ms["parent_session_id"] == team["main_session_id"]


def test_same_agent_twice_are_distinct_instances(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    a = _add_member(client, team["id"], "dev", display_name="dev-前端")
    b = _add_member(client, team["id"], "dev", display_name="dev-后端")
    assert a["instance_id"] != b["instance_id"]
    assert a["session_id"] != b["session_id"]


def test_member_cap(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    from ginno_runtime.teams import store as team_store

    for i in range(team_store.MAX_MEMBERS):
        r = client.post(f"/api/teams/{team['id']}/members",
                        json={"agent_id": "dev", "display_name": f"d{i}"})
        assert r.json().get("ok"), r.json()
    over = client.post(f"/api/teams/{team['id']}/members", json={"agent_id": "dev"})
    assert over.json().get("ok") is False


def test_delete_team_cascades_member_sessions(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev")
    assert client.delete(f"/api/teams/{team['id']}").json()["ok"] is True
    assert client.get(f"/api/teams/{team['id']}").json()["ok"] is False
    sessions = client.get("/api/sessions").json()
    assert all(s["id"] != m["session_id"] for s in sessions)


# --------------------------------------------------------------------------- #
# @ 投递 → 回帖（P0 核心闭环）
# --------------------------------------------------------------------------- #

def test_deliver_runs_member_and_posts_reply(client, patch_build_model):
    """主会话 @dev → dev 在自己 session 跑 → 回帖写回主会话 checkpoint。"""
    _bypass_on()
    # 成员的 turn 用这个脚本：产出一段最终文本 = 回帖正文。
    patch_build_model(_model("dev 改完了：3 个文件，测试 12 passed"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev")

    r = client.post(
        f"/api/teams/{team['id']}/deliver",
        json={"instance_id": m["instance_id"], "text": "把 auth 的 token 刷新改一下"},
    )
    data = r.json()
    assert r.status_code == 200, data
    # 回执三态之一：delivered（有证据）
    assert data["receipt"] in ("delivered", "stalled"), data
    assert data["session_id"] == m["session_id"]

    # 等成员 turn 跑完 + 回帖落地（bg 任务在 TestClient 后台事件循环跑）。
    main_sid = team["main_session_id"]
    deadline = _time.time() + 15
    reply_text = ""
    while _time.time() < deadline:
        hist = json.dumps(client.get(f"/api/sessions/{main_sid}/history").json(),
                          ensure_ascii=False)
        if "12 passed" in hist:
            reply_text = hist
            break
        _time.sleep(0.2)
    assert reply_text, "回帖未出现在主会话 history"

    # 回帖是结构化消息对象：带 agentId + teamReply 标记（§5.2 要点 2）。
    # 内部 kwarg 是 ginno_team_reply，序列化到 bubble 上叫 teamReply。
    assert "teamReply" in reply_text
    assert '"agentId": "dev"' in reply_text
    assert m["instance_id"] in reply_text


def test_deliver_by_agent_id_resolves_first_instance(client, patch_build_model):
    _bypass_on()
    patch_build_model(_model("ok from dev"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev")
    r = client.post(f"/api/teams/{team['id']}/deliver",
                    json={"agent_id": "dev", "text": "hi"})
    data = r.json()
    assert data["receipt"] in ("delivered", "stalled"), data
    assert data["session_id"] == m["session_id"]


def test_deliver_unknown_member_fails(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    r = client.post(f"/api/teams/{team['id']}/deliver",
                    json={"instance_id": "nope", "text": "hi"})
    data = r.json()
    assert data["ok"] is False and data["receipt"] == "failed"
    assert data["reason"] == "unknown_member"


def test_deliver_empty_text_fails(client, patch_build_model):
    patch_build_model(script(text="ok"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev")
    r = client.post(f"/api/teams/{team['id']}/deliver",
                    json={"instance_id": m["instance_id"], "text": "   "})
    data = r.json()
    assert data["receipt"] == "failed" and data["reason"] == "empty_text"


# --------------------------------------------------------------------------- #
# 纪律 1：blocked 时原子拒绝（§5.3.3，herdr v0.8.2）
# --------------------------------------------------------------------------- #

def test_deliver_to_blocked_member_is_atomically_refused(client, patch_build_model):
    """成员停在 interrupt 等人回答时，投递一个字都不发，直接 failed/agent_blocked。"""
    _bypass_on()
    patch_build_model(_model("never runs"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "research")
    sid = m["session_id"]

    # 模拟成员停在权限确认（_PENDING_RESUME 是 set）。
    server_shared._PENDING_RESUME.add(sid)
    server_shared._PENDING_KIND[sid] = "permission"
    try:
        r = client.post(f"/api/teams/{team['id']}/deliver",
                        json={"instance_id": m["instance_id"], "text": "快点给我结论"})
        data = r.json()
        assert data["ok"] is False
        assert data["receipt"] == "failed"
        assert data["reason"] == "agent_blocked"
        # 原子性证据：steer 队列里没有塞进任何东西
        assert not server_shared._STEER_STASH.get(sid)
    finally:
        server_shared._PENDING_RESUME.discard(sid)
        server_shared._PENDING_KIND.pop(sid, None)


# --------------------------------------------------------------------------- #
# 状态回流 + seen（§3.3 / §5.3）
# --------------------------------------------------------------------------- #

def test_member_status_working_then_done_then_idle(client, patch_build_model):
    """working → done（未读）→ 点进工作台 seen → idle。"""
    _bypass_on()
    patch_build_model(_model("done work"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev")
    iid = m["instance_id"]

    client.post(f"/api/teams/{team['id']}/deliver",
                json={"instance_id": iid, "text": "干活"})

    # 等 turn 结束、状态落 done（未读）。
    deadline = _time.time() + 15
    status = ""
    while _time.time() < deadline:
        status = _member_row(_get_team(client, team["id"]), "dev")["status"]
        if status == "done":
            break
        _time.sleep(0.2)
    assert status == "done", f"成员应转 done（未读），实际 {status}"
    assert _member_row(_get_team(client, team["id"]), "dev")["unread"] is True
    # done 冒泡到 needs_action（§5.3.1）
    assert _get_team(client, team["id"])["rollup"]["needs_action"] is True

    # 用户打开工作台 → seen → idle
    assert client.post(f"/api/teams/{team['id']}/members/{iid}/seen").json()["ok"]
    row = _member_row(_get_team(client, team["id"]), "dev")
    assert row["status"] == "idle" and row["unread"] is False


def test_seen_is_per_client(client, patch_build_model):
    """主窗口 seen 不影响 pin 浮窗的未读（§3.3，herdr 每客户端独立记）。"""
    _bypass_on()
    patch_build_model(_model("done work"))
    team = _create_team(client)
    m = _add_member(client, team["id"], "dev")
    iid = m["instance_id"]
    client.post(f"/api/teams/{team['id']}/deliver", json={"instance_id": iid, "text": "干活"})

    deadline = _time.time() + 15
    while _time.time() < deadline:
        if _member_row(_get_team(client, team["id"], "main"), "dev")["status"] == "done":
            break
        _time.sleep(0.2)

    client.post(f"/api/teams/{team['id']}/members/{iid}/seen?client=main")
    assert _member_row(_get_team(client, team["id"], "main"), "dev")["status"] == "idle"
    # pin 客户端还没看过 → 仍是 done
    assert _member_row(_get_team(client, team["id"], "pin"), "dev")["status"] == "done"


# --------------------------------------------------------------------------- #
# WS 路由：主会话 @agent → 投递分支（§5.2 步骤 2）
# --------------------------------------------------------------------------- #

def test_ws_agent_mention_in_team_main_routes_to_delivery(client, create_session, ws_conv, patch_build_model):
    """在团队主会话里 @dev 不再换 persona，而是投递给 dev 的 session。

    断言：主会话**没有**因此起 turn（消息被投递走了），且 dev 的 session 收到了。
    """
    _bypass_on()
    patch_build_model(_model("dev handled it"))

    # 主会话由建团队时创建；这里手动把它的 agent 设为 dev 以便 @ 解析命中。
    team = _create_team(client)
    main_sid = team["main_session_id"]
    m = _add_member(client, team["id"], "dev")

    with ws_conv(main_sid) as conv:
        conv.invoke(
            "把 token 刷新改一下",
            mentions=[{"kind": "agent", "id": "dev"}],
        )
        # 投递分支发 team.delivered 事件后 continue，不进 turn（无 message.end）。
        # 注意：deliver 会先广播 team.changed（状态回流），所以用 recv_until
        # 收到 team.delivered 为止，不假设它是第一个事件。
        events = conv.recv_until("team.delivered", "message.end", "error")
        names = [e.get("event") for e in events]
        assert "team.delivered" in names, names
        # 关键：投递分支**没有**起主会话 turn（消息被投递走了）
        assert "message.end" not in names, names
        ev = next(e for e in events if e.get("event") == "team.delivered")
        assert ev.get("receipt") in ("delivered", "stalled"), ev
        assert ev.get("agent_id") == "dev"

    # dev 的 session 最终跑出回帖（等 bg turn）。
    deadline = _time.time() + 15
    got = ""
    while _time.time() < deadline:
        hist = json.dumps(client.get(f"/api/sessions/{main_sid}/history").json(),
                          ensure_ascii=False)
        if "dev handled it" in hist:
            got = hist
            break
        _time.sleep(0.2)
    assert got, "回帖未回到主会话"
    assert m["instance_id"] in got


def test_ws_agent_mention_in_plain_session_keeps_persona_semantics(client, create_session, ws_conv, patch_build_model):
    """回归：非团队会话里 @agent 仍是旧的 persona 覆盖语义，行为不变。"""
    _bypass_on()
    patch_build_model(_model("plain reply"))
    sid = create_session([script(text="plain reply")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello", mentions=[{"kind": "agent", "id": "research"}])
        events = conv.recv_until("message.end", "error")
    names = [e.get("event") for e in events]
    # 走的是普通 turn（有 message.end），不是 team.delivered
    assert "message.end" in names
    assert "team.delivered" not in names


def test_create_team_appoints_entity_leader(client, patch_build_model):
    """建队选了 Lead persona → 必有实体 leader 成员实例（2026-10-14 服务端保证）。

    HiHi/DDD 存量缺领队的根因即旧版只写主会话 persona——现在 create 端点
    直接任命，前端不参与。
    """
    patch_build_model(script(text="ok"))
    r = client.post("/api/teams", json={"name": "L", "agent_id": "dev"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"], body
    team = body["team"]
    assert team["leader_instance_id"]
    leaders = [m for m in team["members"] if m["role"] == "leader"]
    assert len(leaders) == 1
    assert leaders[0]["agent_id"] == "dev"
    # 领队有自己的独立会话（≠ 主会话）
    assert leaders[0]["session_id"]
    assert leaders[0]["session_id"] != team["main_session_id"]
