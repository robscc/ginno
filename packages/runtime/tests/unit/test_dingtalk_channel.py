"""DingTalk channel 单元测试：Connector 注册 + DingTalkTap 帧翻译（headless，
不依赖 langchain/FastAPI —— 与 test_dingtalk_local.py 同样可用 --noconftest 跑）。

覆盖设计（docs/dingtalk-connector-design.md）：§2.2 config_schema、F3 流式节流
与终态幂等、F4 问题卡片与超时 skip、F7 产物上行降级、§5 分发表忽略项。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.channels.dingtalk.tap import DingTalkTap, _widget_to_markdown

# ---------------------------------------------------------------- Connector

def test_connector_registered_and_schema():
    from ginno_runtime.connectors.builtin import (
        DingTalkConnector,
        ensure_builtin_connectors,
    )
    from ginno_runtime.connectors.registry import registry

    ensure_builtin_connectors()
    conn = registry().get("dingtalk-bot")
    assert isinstance(conn, DingTalkConnector)
    cfg = conn.config_schema()["properties"]
    for key in ("app_key", "app_secret", "allowed_staff_ids", "menus",
                "ask_timeout_s", "tool_policy", "card_template_id"):
        assert key in cfg, key
    # 默认关闭 + 高危确认策略（§2.2 / F11）
    default = conn.default_config()
    assert default["enabled"] is False
    assert default["tool_policy"] == "confirm_highrisk"


def test_default_agent_field_dropdown(monkeypatch):
    """default_agent_id 是下拉：enum 动态取自 agent registry，"" = 全局默认
    （与 WeCom 共用 _default_agent_field，这里只验接入）。"""
    from types import SimpleNamespace

    from ginno_runtime import agents as agents_reg
    from ginno_runtime.connectors.builtin import DingTalkConnector

    monkeypatch.setattr(agents_reg, "list_agents", lambda: [
        SimpleNamespace(id="dev", name="Dev"),
    ])
    spec = DingTalkConnector().config_schema()["properties"]["default_agent_id"]
    assert spec["enum"] == ["", "dev"]
    assert spec["x-ginno-enum-labels"][""] == "Auto (default agent)"


def test_connector_apply_config_disabled_is_noop():
    """enabled=False → apply_config 直接返回（无 adapter 可停也不炸）。"""
    from ginno_runtime.connectors.builtin import DingTalkConnector

    asyncio.run(DingTalkConnector().apply_config({"enabled": False}))


# ---------------------------------------------------------------- fakes

class FakeCards:
    def __init__(self):
        self.stream_card = object()
        self.created = 0
        self.updates: list[str] = []
        self.finalized: list[tuple[object, str, str]] = []
        self.questions: list[dict] = []
        self.disabled: list[str] = []

    async def create_stream_card(self, staff_id, placeholder="…"):
        self.created += 1
        return self.stream_card

    async def stream_update(self, card, md):
        self.updates.append(md)

    async def finalize(self, card, md, status="done"):
        self.finalized.append((card, md, status))

    async def send_question_card(self, staff_id, q):
        self.questions.append(q)
        return str(q["id"])

    async def disable_question_card(self, qid):
        self.disabled.append(qid)


class FakeAdapter:
    """tap 依赖的最小 adapter 面（cards / api / config / send_markdown / 问题登记）。"""

    def __init__(self, config=None):
        self.config = config or {"ask_timeout_s": 600}
        self.cards = FakeCards()
        self.markdowns: list[tuple[str, str, str]] = []
        self.registered: dict[str, str] = {}

    async def send_markdown(self, staff_id, title, text):
        self.markdowns.append((staff_id, title, text))

    async def send_media(self, *a, **kw):
        raise AssertionError("not expected in these tests")

    async def register_question(self, qid, sid):
        self.registered[qid] = sid

    async def unregister_question(self, qid):
        self.registered.pop(qid, None)


def _frame(event: str, **data) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False)


# ---------------------------------------------------------------- F3 流式

async def test_tap_streams_and_finalizes_once():
    ad = FakeAdapter()
    tap = DingTalkTap(ad, "s1", "cid1")
    tap.session_id = "sess1"
    await tap.send_text(_frame("turn.start"))
    assert ad.cards.created == 1
    for i in range(3):
        await tap.send_text(_frame("token.delta", content=f"段落{i}。"))
    # 首批立即流出（与 WeCom tap 同语义：卡片创建后立刻见首响应），
    # 其余进节流窗口
    assert len(ad.cards.updates) == 1 and "段落0" in ad.cards.updates[0]
    await tap.send_text(_frame("message.end"))
    await tap.close()  # 终态 flush + finalize，且幂等
    assert len(ad.cards.updates) == 2
    assert "段落2" in ad.cards.updates[1]
    assert len(ad.cards.finalized) == 1
    body = ad.cards.finalized[0][1]
    assert "段落0" in body and "段落2" in body


async def test_tap_progress_section_streams_and_clears():
    """P1 进展区：thinking/tool 帧进引用块随卡片流更新；终稿 finalize 只留
    正文，不带进展区。"""
    ad = FakeAdapter()
    tap = DingTalkTap(ad, "s1", "cid1")
    tap.session_id = "sess1"
    await tap.send_text(_frame("turn.start"))
    await tap.send_text(_frame("tool.start", name="web_search", id="t9"))
    assert any("> 🔧 查询资料：web_search" in u for u in ad.cards.updates)
    tap._last_flush = 0.0  # 测试便利：绕过节流窗口看中间帧
    await tap.send_text(_frame("tool.end", id="t9", content="3 条结果"))
    assert any("✔ web_search：3 条结果" in u for u in ad.cards.updates)
    await tap.send_text(_frame("token.delta", content="今天晴"))
    await tap.send_text(_frame("message.end"))
    await tap.close()
    body = ad.cards.finalized[0][1]
    assert "今天晴" in body
    assert "🔧" not in body and "🧠" not in body and "✔" not in body


async def test_tap_card_failure_degrades_to_markdown():
    ad = FakeAdapter()

    async def boom(*a, **kw):
        raise RuntimeError("card api down")

    ad.cards.create_stream_card = boom  # type: ignore[method-assign]
    tap = DingTalkTap(ad, "s1", "cid1")
    await tap.send_text(_frame("turn.start"))
    await tap.send_text(_frame("token.delta", content="正文"))
    await tap.close()
    # 降级：终态走一条 markdown 消息保证可见
    assert ad.markdowns and "正文" in ad.markdowns[0][2]


# ---------------------------------------------------------------- F4 问题

async def test_tap_question_card_and_timeout_skip(monkeypatch):
    # resume_pending_interrupt 所在的 api.stream 包在 __init__ 期 re-export
    # engine（langchain_core）——需要完整运行时环境；轻环境跳过本用例。
    pytest.importorskip("langchain_core")
    ad = FakeAdapter({"ask_timeout_s": 0.05})
    tap = DingTalkTap(ad, "s1", "cid1")
    tap.session_id = "sess1"

    resumed: list[tuple[str, dict]] = []

    import ginno_runtime.server_shared as shared

    shared._PENDING_RESUME.add("sess1")
    shared._PENDING_KIND["sess1"] = "user_question"

    import ginno_runtime.api.stream.resume as resume_mod

    monkeypatch.setattr(resume_mod, "_spawn_resume_task",
                        lambda sid, val, cfg: resumed.append((sid, val)))
    try:
        await tap.send_text(_frame(
            "user.question", id="q1", question="选哪个？", header="选择",
            options=["A", "B"], allow_free_text=True))
        assert ad.cards.questions[0]["options"] == ["A", "B"]
        assert ad.registered.get("q1") == "sess1"
        await asyncio.sleep(0.12)  # 超过 ask_timeout_s
        assert ad.cards.disabled == ["q1"]
        assert resumed and resumed[0][1]["skip"] is True
    finally:
        shared._PENDING_RESUME.discard("sess1")
        shared._PENDING_KIND.pop("sess1", None)


# ---------------------------------------------------------------- F7 产物

async def test_tap_image_emit_uploads(tmp_path: Path, monkeypatch):
    ad = FakeAdapter()
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG fake")
    tap = DingTalkTap(ad, "s1", "cid1")

    uploaded: list[tuple[Path, str]] = []

    class FakeApi:
        async def upload_media(self, p, type="file"):
            uploaded.append((p, type))
            return "media-1"

    ad.api = FakeApi()

    async def fake_send_media(staff_id, media_id, *, file_name, file_ext, is_image):
        assert media_id == "media-1" and is_image

    ad.send_media = fake_send_media  # type: ignore[method-assign]

    import ginno_runtime.files as files_mod

    # 不真正注册文件 registry：monkeypatch get_by_id 返回带 path 的假 entry
    class _E:
        name = "chart.png"
        path = str(img)

    monkeypatch.setattr(files_mod, "get_by_id", lambda fid: _E() if fid == "f1" else None)
    await tap.send_text(_frame("image.emit", file_id="f1", name="chart.png"))
    assert uploaded and uploaded[0][1] == "image"
    # 未知 file_id / 不存在文件 → 静默
    await tap.send_text(_frame("image.emit", file_id="nope", name="x.png"))


# ---------------------------------------------------------------- 降级渲染

def test_widget_to_markdown():
    md = _widget_to_markdown({"kind": "stat_list", "data": {
        "title": "状态", "items": [
            {"label": "调研", "value": "完成", "status": "done"},
            {"label": "实现", "value": "进行中", "status": "running"},
        ]}})
    assert "状态" in md and "✅" in md and "⏳" in md
    md2 = _widget_to_markdown({"kind": "chart", "data": {
        "type": "bar", "title": "T", "x": "m", "y": "n",
        "data": [{"m": "a", "n": 1}, {"m": "b", "n": 2}]}})
    assert "| a | 1 |" in md2
    assert _widget_to_markdown({"kind": "unknown"}) == ""


def test_tap_ignores_noise_frames():
    ad = FakeAdapter()
    tap = DingTalkTap(ad, "s1", "cid1")
    for ev in ("thinking.delta", "tool.start", "tool.end", "keepalive",
               "artifacts.changed", "run.bind", "preview.invalidate"):
        asyncio.get_event_loop_policy()
        asyncio.run(tap.send_text(_frame(ev, content="x", name="bash")))
    assert ad.cards.updates == [] and ad.markdowns == []


# ---------------------------------------------------------------- 删除通知


async def test_notify_session_deleted_unbinds_and_notifies(tmp_path: Path):
    """桌面端删除渠道会话 → 解绑该 session 的全部映射并通知成员（去重）；
    其余映射保留。"""
    from ginno_runtime.channels.dingtalk.adapter import DingTalkChannelAdapter
    from ginno_runtime.channels.dingtalk.sessions import SessionMapper

    ad = DingTalkChannelAdapter()
    ad.config = {}
    mapper = SessionMapper(tmp_path / "m.json")
    mapper.load()
    ad._mapper = mapper
    sent: list[str] = []

    async def fake_send(staff_id, title, text):
        sent.append(staff_id)

    ad.send_markdown = fake_send  # type: ignore[method-assign]
    mapper.bind(staff_id="s1", conversation_id="cid1", ginno_session="gone1")
    mapper.bind(staff_id="s1", conversation_id="cid2", ginno_session="gone1")
    mapper.bind(staff_id="s2", conversation_id="cid3", ginno_session="keep1")
    await ad.notify_session_deleted("gone1")
    assert mapper.chats_for("gone1") == []
    assert mapper.get(staff_id="s2", conversation_id="cid3") == "keep1"
    # 同一成员多条映射去重，只发一条
    assert sent == ["s1"]


async def test_notify_turn_reply_dedupes_members(tmp_path: Path):
    """无 tap 的 turn 收口补推：绑定成员各一条（同成员去重）、带 📬 前缀；
    未绑定不投。"""
    from ginno_runtime.channels.dingtalk.adapter import DingTalkChannelAdapter
    from ginno_runtime.channels.dingtalk.sessions import SessionMapper

    ad = DingTalkChannelAdapter()
    ad.config = {}
    mapper = SessionMapper(tmp_path / "m.json")
    mapper.load()
    ad._mapper = mapper
    sent: list[tuple[str, str]] = []

    async def fake_send(staff_id, title, text):
        sent.append((staff_id, text))

    ad.send_markdown = fake_send  # type: ignore[method-assign]
    mapper.bind(staff_id="s1", conversation_id="cid1", ginno_session="sub1")
    mapper.bind(staff_id="s1", conversation_id="cid2", ginno_session="sub1")
    await ad.notify_turn_reply("sub1", "订阅的任务已完成：结论 A")
    assert sent == [("s1", "📬 订阅的任务已完成：结论 A")]
    sent.clear()
    await ad.notify_turn_reply("never-bound", "hi")
    assert sent == []
