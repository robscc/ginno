"""P2-3：渠道消息投递团队（backlog P2-3）。

渠道会话绑定团队主会话时，消息应投给成员而不是在广场自跑 headless turn。
渠道是请求/响应模型 → 必须同步等成员产出再经 tap 回一条 IM 消息。
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.channels.base import _run_channel_team_turn
from ginno_runtime.teams import store as team_store
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


def _model(*texts):
    return ScriptedChatModel(scripts=[script(text=t) for t in texts])


class FakeTap:
    """最小 ChannelTap：只记录 send_text 的 payload。"""

    def __init__(self):
        self.sent: list[str] = []
        self.session_id = ""

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)

    async def on_frame(self, event: str, data: dict) -> None:
        pass

    def texts(self) -> list[str]:
        out = []
        for p in self.sent:
            try:
                out.append(str(json.loads(p).get("text") or ""))
            except Exception:  # noqa: BLE001
                out.append(p)
        return out


async def test_channel_team_delivers_and_replies_member_output(client, patch_build_model):
    patch_build_model(_model("成员产出：巡检完成，一切正常"))
    t = client.post("/api/teams", json={
        "name": "渠道队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    tap = FakeTap()
    out = await _run_channel_team_turn(t, "巡检一下", tap, t["main_session_id"])
    assert out == []
    assert any("巡检完成" in x for x in tap.texts()), tap.texts()
    # 成员真的跑了
    m = team_store.get_member(t["id"], t["leader_instance_id"])
    assert m["status"] in ("done", "idle", "working"), m


async def test_channel_team_empty_team_replies_hint(client, patch_build_model):
    patch_build_model(_model("ok"))
    t = client.post("/api/teams", json={"name": "空队", "project_slug": "default"}).json()["team"]
    tap = FakeTap()
    await _run_channel_team_turn(t, "hi", tap, t["main_session_id"])
    assert any("成员" in x for x in tap.texts()), tap.texts()


async def test_channel_team_blocked_member_refused_not_silent(client, patch_build_model):
    """blocked 原子拒绝 → 如实回「被卡住」，不静默吞也不假装已处理。"""
    from ginno_runtime import server_shared

    patch_build_model(_model("ok"))
    t = client.post("/api/teams", json={
        "name": "卡住队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    m = team_store.get_member(t["id"], t["leader_instance_id"])
    server_shared._PENDING_RESUME.add(m["session_id"])
    try:
        tap = FakeTap()
        await _run_channel_team_turn(t, "干活", tap, t["main_session_id"])
        joined = " ".join(tap.texts())
        assert joined.strip(), "拒绝时必须回一条消息，不能静默"
        low = joined.lower()
        # 双语断言（isolated_home pin en，但文案可能随 locale 变）
        assert any(k in low for k in (
            "refused", "blocked", "stopped", "卡", "拒绝",
        )), joined
    finally:
        server_shared._PENDING_RESUME.discard(m["session_id"])


async def test_channel_route_only_for_team_main_not_member(client, patch_build_model):
    """成员会话（工作台）走原 headless 路径，不得被团队路由劫持。"""
    patch_build_model(_model("ok"))
    t = client.post("/api/teams", json={
        "name": "队", "project_slug": "default", "agent_id": "dev",
    }).json()["team"]
    m = client.post(f"/api/teams/{t['id']}/members",
                    json={"agent_id": "writer"}).json()["member"]
    # team_of_session 对成员会话返回 (team, member) —— member 非 None
    located = team_store.team_of_session(m["session_id"])
    assert located is not None and located[1] is not None
    # 主会话才是 member is None
    located_main = team_store.team_of_session(t["main_session_id"])
    assert located_main is not None and located_main[1] is None
