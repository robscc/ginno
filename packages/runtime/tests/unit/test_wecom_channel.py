"""WeCom channel 单元测试：Connector 注册 + 协议层 + WeComTap 帧翻译
（headless，不依赖 langchain/FastAPI —— 与 test_dingtalk_local.py 同样可用
--noconftest 跑）。

覆盖设计（docs/wecom-connector-design.md）：协议帧构造/解析、F3 流式节流
与终态幂等、F4 文本作答与超时 skip、媒体解密、F2 会话映射、在途锁提示。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.channels.wecom import protocol as p
from ginno_runtime.channels.wecom.tap import WeComTap, _widget_to_markdown

# ---------------------------------------------------------------- Connector


def test_connector_registered_and_schema():
    from ginno_runtime.connectors.builtin import (
        WeComConnector,
        ensure_builtin_connectors,
    )
    from ginno_runtime.connectors.registry import registry

    ensure_builtin_connectors()
    conn = registry().get("wecom-bot")
    assert isinstance(conn, WeComConnector)
    cfg = conn.config_schema()["properties"]
    for key in ("bot_id", "secret", "allowed_userids", "default_agent_id",
                "session_policy", "ask_timeout_s", "welcome_text"):
        assert key in cfg, key
    assert cfg["secret"]["format"] == "password"
    default = conn.default_config()
    assert default["enabled"] is False
    assert default["session_policy"] == "per_user"


def test_default_agent_field_dropdown(monkeypatch):
    """default_agent_id 是下拉：enum 动态取自 agent registry，"" = 全局默认。"""
    from types import SimpleNamespace

    from ginno_runtime import agents as agents_reg
    from ginno_runtime.connectors.builtin import WeComConnector

    monkeypatch.setattr(agents_reg, "list_agents", lambda: [
        SimpleNamespace(id="dev", name="Dev"),
        SimpleNamespace(id="writer", name="写手"),
        SimpleNamespace(id="bare", name=""),
    ])
    spec = WeComConnector().config_schema()["properties"]["default_agent_id"]
    assert spec["enum"] == ["", "dev", "writer", "bare"]
    labels = spec["x-ginno-enum-labels"]
    assert labels[""] == "Auto (default agent)"
    assert labels["dev"] == "Dev (dev)"
    assert labels["writer"] == "写手 (writer)"
    assert labels["bare"] == "bare"  # 无名 agent 回退显示 id


def test_default_agent_field_degrades_to_text(monkeypatch):
    """agents registry 读炸 → 退回纯文本字段，详情页不受影响。"""
    from ginno_runtime import agents as agents_reg
    from ginno_runtime.connectors.builtin import WeComConnector

    def boom():
        raise RuntimeError("agents dir gone")

    monkeypatch.setattr(agents_reg, "list_agents", boom)
    spec = WeComConnector().config_schema()["properties"]["default_agent_id"]
    assert spec["type"] == "string"
    assert "enum" not in spec


def test_channel_mark_backfill():
    """渠道标记：mark_channel_sessions 存量迁移（幂等、跳过已删会话、
    不 bump updated），mark_channel_meta 认内存条目的 session_id 主键。"""
    from ginno_runtime import paths
    from ginno_runtime.channels.base import mark_channel_sessions
    from ginno_runtime.session_meta import (
        _find_meta,
        _session_meta_upsert,
        mark_channel_meta,
    )

    slug = "default"
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    _session_meta_upsert(slug, {"id": "old1", "title": "存量会话"})
    assert mark_channel_sessions("wecom", ["old1", "nosuch"]) == 1
    m, found_slug = _find_meta("old1")
    assert m["type"] == "channel" and m["channel"] == "wecom"
    assert found_slug == slug
    assert "updated" not in m  # touch=False：补标记不动侧栏排序
    # 幂等 + 已删除会话忽略
    assert mark_channel_sessions("wecom", ["old1", "nosuch"]) == 0
    # 内存条目（主键 session_id）同样可标
    mem = {"session_id": "old1"}
    mark_channel_meta(mem, "wecom")
    assert mem["type"] == "channel" and mem["channel"] == "wecom"


def test_ensure_channel_session_deleted_id_falls_through(monkeypatch):
    """桌面端删除渠道会话后映射表仍指向旧 id：meta 已不在时回落新建
    （渠道「删了再聊」应自愈开新会话，而不是报「会话创建失败」）；
    meta 还在但重建失败（模型/凭证问题）时维持报错，不静默换新会话。"""
    import ginno_runtime.api.sessions as sessions_mod
    import ginno_runtime.session_meta as session_meta
    from ginno_runtime.channels.base import ensure_channel_session

    async def fake_create(req):
        return {"session_id": "new1"}

    monkeypatch.setattr(sessions_mod, "create_session", fake_create)
    # 已删除路径：_ensure_session 找不到、meta 也没有 → 新建
    monkeypatch.setattr(sessions_mod, "_ensure_session",
                        lambda sid: {"session_id": sid} if sid == "new1" else None)
    monkeypatch.setattr(session_meta, "_find_meta", lambda sid: None)
    session, sid = asyncio.run(ensure_channel_session("ghost", channel="wecom"))
    assert sid == "new1" and session == {"session_id": "new1"}

    # meta 还在但重建失败：维持报错，返回旧 id 便于日志定位
    monkeypatch.setattr(session_meta, "_find_meta", lambda sid: ({"id": "ghost"}, "default"))
    session, sid = asyncio.run(ensure_channel_session("ghost", channel="wecom"))
    assert session is None and sid == "ghost"


def test_notify_session_deleted_unbinds_and_pushes(tmp_path: Path):
    """桌面端删除渠道会话 → 解绑该 session 的全部映射并逐 chat 推送提示；
    client 断开时只解绑不发送；未绑定的 session 不发不报。"""
    ad = _make_adapter(tmp_path, {})
    ad._mapper.bind(user_id="zhang", chat_id="czhang", ginno_session="gone1")
    ad._mapper.bind(user_id="lisi", chat_id="wrChat1", ginno_session="gone1")
    ad._mapper.bind(user_id="wang", chat_id="wrChat2", ginno_session="keep1")
    asyncio.run(ad.notify_session_deleted("gone1"))
    # 只解绑目标 session，其余映射保留
    assert ad._mapper.get(user_id="wang", chat_id="wrChat2") == "keep1"
    assert ad._mapper.chats_for("gone1") == []
    assert {cid for cid, _ in ad.client.pushes} == {"czhang", "wrChat1"}
    assert all("已在桌面端删除" in c for _, c in ad.client.pushes)
    # 未绑定 session：无映射即无动作
    ad.client.pushes.clear()
    asyncio.run(ad.notify_session_deleted("never-bound"))
    assert ad.client.pushes == []
    # client 断开：只解绑，不发送、不抛
    ad2 = _make_adapter(tmp_path / "b", {})
    ad2._mapper.bind(user_id="zhang", chat_id="czhang", ginno_session="gone1")
    ad2.client = None
    asyncio.run(ad2.notify_session_deleted("gone1"))
    assert ad2._mapper.chats_for("gone1") == []


def test_notify_channel_session_deleted_no_adapter(monkeypatch):
    """渠道未启用/无适配器时 base 反向通知静默跳过，不抛。"""
    import ginno_runtime.connectors.builtin as builtin_mod
    from ginno_runtime.channels.base import notify_channel_session_deleted

    monkeypatch.setattr(builtin_mod, "channel_adapter", lambda ch: None)
    asyncio.run(notify_channel_session_deleted("wecom", "nosuch"))
    asyncio.run(notify_channel_session_deleted(None, "x"))
    asyncio.run(notify_channel_session_deleted("nosuch-channel", "x"))


def test_connector_apply_config_disabled_is_noop():
    from ginno_runtime.connectors.builtin import WeComConnector

    asyncio.run(WeComConnector().apply_config({"enabled": False}))


# ---------------------------------------------------------------- protocol


def test_frame_and_stream_body():
    f = p.frame(p.CMD_SUBSCRIBE, {"bot_id": "b", "secret": "s"})
    assert f["cmd"] == p.CMD_SUBSCRIBE
    assert f["headers"]["req_id"].startswith("aibot_subscribe_")
    body = p.stream_body("sid", "你好", finish=True)
    assert body == {"msgtype": "stream",
                    "stream": {"id": "sid", "content": "你好", "finish": True}}
    assert "finish" not in p.stream_body("sid", "hi")


def test_parse_frame_ack_and_callback():
    ack = p.parse_frame(json.dumps(
        {"headers": {"req_id": "r1"}, "errcode": 0, "errmsg": "ok"}))
    assert ack["errcode"] == 0 and ack["cmd"] is None
    cb = p.parse_frame(json.dumps({
        "cmd": "aibot_msg_callback", "headers": {"req_id": "r2"},
        "body": {"msgid": "m", "msgtype": "text"}}))
    assert cb["cmd"] == p.CMD_MSG_CALLBACK and cb["errcode"] is None
    assert p.parse_frame("not json") == {}
    assert p.parse_frame("[1,2]") == {}


def test_message_helpers():
    body = {
        "msgtype": "text", "chattype": "group", "chatid": "wrChat",
        "from": {"userid": "zhang"}, "text": {"content": "@Ginno 你好"},
        "compat_info": {"source": "channel"},
    }
    assert p.msg_user_id(body) == "zhang"
    assert p.msg_chat_key(body) == "wrChat"
    assert p.msg_chat_key({"from": {"userid": "zhang"}}) == "zhang"
    assert p.msg_is_channel(body) is True
    assert p.is_mention_text("@Ginno 你好") is True
    assert p.strip_mention_prefix("@Ginno @Bot 帮我查天气") == "帮我查天气"
    # 群聊 @ 后的分隔符是 U+2005 窄空格，不是 ASCII 空格
    assert p.strip_mention_prefix("@Ginno 查下天气") == "查下天气"
    assert p.is_mention_text("@Ginno 查下天气") is True
    assert p.strip_mention_prefix("普通消息") == "普通消息"
    assert p.extract_text(body) == "@Ginno 你好"
    text, image = p.extract_mixed({"mixed": {"msg_item": [
        {"msgtype": "text", "text": {"content": "看这张"}},
        {"msgtype": "image", "image": {"url": "https://x", "aeskey": "k"}},
    ]}})
    assert text == "看这张" and image == {"url": "https://x", "aeskey": "k"}


def test_parse_answer():
    skip = p.parse_answer("跳过", ["A", "B"])
    assert skip["skip"] is True
    opt = p.parse_answer("2", ["A", "B"])
    assert opt["option_index"] == 1 and opt["answer"] == "B"
    free = p.parse_answer("随便都行", ["A", "B"])
    assert free["answer"] == "随便都行" and free["option_index"] is None
    oob = p.parse_answer("9", ["A", "B"])
    assert oob["answer"] == "9"  # 越界数字按自由文本


# ---------------------------------------------------------------- sessions


def test_session_mapper_roundtrip(tmp_path: Path):
    from ginno_runtime.channels.wecom.sessions import SessionMapper

    m = SessionMapper(tmp_path / "mappings.json")
    m.load()
    assert m.get(user_id="u1", chat_id="c1") is None
    m.bind(user_id="u1", chat_id="c1", ginno_session="s1", nick="张三")
    m.bind(user_id="u1", chat_id="c1", ginno_session="s2")  # upsert
    assert m.get(user_id="u1", chat_id="c1") == "s2"
    m.unbind("s2")
    assert m.get(user_id="u1", chat_id="c1") is None
    # 损坏文件重建
    (tmp_path / "m2.json").write_text("{broken", encoding="utf-8")
    m2 = SessionMapper(tmp_path / "m2.json")
    m2.load()
    assert m2.all() == []


# ---------------------------------------------------------------- media


def test_decrypt_media_roundtrip():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from ginno_runtime.channels.wecom.media import decrypt_media

    key = base64.b64encode(b"k" * 32).decode()
    plain = b"hello wecom file" * 10
    pad_len = 16 - len(plain) % 16
    padded = plain + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(b"k" * 32), modes.CBC(b"k" * 16)).encryptor()
    data = enc.update(padded) + enc.finalize()
    assert decrypt_media(data, key) == plain
    # 未加密内容兜底：不是合法填充时原样返回
    assert decrypt_media(b"\x01\x02\x03", key) == b"\x01\x02\x03"
    # 真机 aeskey 形状（2026-10-28 实录 Incorrect padding）：无填充 43 字符、
    # 以及 URL-safe 字母表——_b64decode_loose 归一化后照常解密。
    assert decrypt_media(data, key.rstrip("=")) == plain
    urlsafe = key.rstrip("=").replace("+", "-").replace("/", "_")
    assert decrypt_media(data, urlsafe) == plain


# ---------------------------------------------------------------- fakes


class FakeClient:
    def __init__(self):
        self.responds: list[tuple[str, dict]] = []
        self.pushes: list[tuple[str, str]] = []

    async def respond(self, req_id: str, body: dict) -> dict:
        self.responds.append((req_id, body))
        return {}

    async def respond_welcome(self, req_id: str, content: str) -> dict:
        self.responds.append((req_id, {"msgtype": "text",
                                       "text": {"content": content}}))
        return {}

    async def send_markdown(self, chatid: str, content: str) -> dict:
        self.pushes.append((chatid, content))
        return {}


class FakeAdapter:
    """tap 依赖的最小 adapter 面（client / config / 待答登记）。"""

    def __init__(self, config=None):
        self.config = config or {"ask_timeout_s": 600}
        self.client = FakeClient()
        self.pending: dict[str, dict] = {}

    async def register_pending_question(self, chat_key, session_id, qid, options):
        self.pending[chat_key] = {
            "session_id": session_id, "question_id": qid, "options": options}

    async def unregister_pending_question(self, chat_key):
        self.pending.pop(chat_key, None)


def _frame(event: str, **data) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False)


# ---------------------------------------------------------------- F3 流式


async def test_tap_streams_and_finalizes_once():
    ad = FakeAdapter()
    tap = WeComTap(ad, "req1", "u1", "stream1")
    tap.session_id = "sess1"
    for i in range(3):
        await tap.send_text(_frame("token.delta", content=f"段落{i}。"))
    # 首批立即流出（紧接占位帧），其余进节流窗口
    assert len(ad.client.responds) == 1
    assert "段落0" in ad.client.responds[0][1]["stream"]["content"]
    await tap.close()  # 终态 flush + finish 定格，且幂等
    streams = [b["stream"] for _, b in ad.client.responds]
    assert len(ad.client.responds) == 3  # 首批 + 收尾全量 + finish
    final = streams[-1]
    assert final["finish"] is True and final["id"] == "stream1"
    assert "段落0" in final["content"] and "段落2" in final["content"]
    assert ad.client.pushes == []


async def test_tap_progress_section_streams_and_clears():
    """P1 进展区：thinking/tool 帧进引用块随流更新；正文出现收思考行；
    终稿（finish 帧）只留正文，不带进展区。"""
    ad = FakeAdapter()
    tap = WeComTap(ad, "req1", "u1", "stream1")
    tap.session_id = "sess1"
    await tap.send_text(_frame("tool.start", name="read_file", id="t1"))
    # 首个进展事件立即出帧（_last_flush 初值 -inf）
    mid = [b for _, b in ad.client.responds if b.get("stream")]
    assert any("> 🔧 检查文件：read_file" in s["stream"]["content"] for s in mid)
    tap._last_flush = float("-inf")  # 测试便利：绕过节流窗口看中间帧
    await tap.send_text(_frame("tool.end", id="t1", content="共 120 行\n第二行"))
    mid = [b for _, b in ad.client.responds if b.get("stream")]
    # 结果摘要只取首行
    assert any("✔ read_file：共 120 行" in s["stream"]["content"]
               for s in mid if not s["stream"].get("finish"))
    await tap.send_text(_frame("token.delta", content="结论如下"))
    await tap.close()
    streams = [b["stream"] for _, b in ad.client.responds]
    final = streams[-1]
    assert final["finish"] is True
    assert "结论如下" in final["content"]
    assert "🔧" not in final["content"] and "🧠" not in final["content"]


async def test_tap_stream_deadline_falls_back_to_push(monkeypatch):
    """超过 9.5 分钟安全线：强制 finish，残余增量走主动推送。"""
    ad = FakeAdapter()
    tap = WeComTap(ad, "req1", "u1", "stream1")
    # 把起点拨回 10 分钟前，强制触发安全线
    tap._t0 = tap._t0 - (p.STREAM_SAFE_S + 60)
    await tap.send_text(_frame("token.delta", content="很长的输出"))
    # 安全线已强制 finish；此后到达的增量走主动推送缓冲
    await tap.send_text(_frame("token.delta", content="，这是残余部分"))
    await tap.close()
    finishes = [b for _, b in ad.client.responds if b.get("stream", {}).get("finish")]
    assert len(finishes) == 1
    assert "内容较长" in finishes[0]["stream"]["content"]
    assert ad.client.pushes and "残余部分" in ad.client.pushes[0][1]


# ---------------------------------------------------------------- F4 问题


async def test_tap_question_registers_pending_and_timeout_skip(monkeypatch):
    pytest.importorskip("langchain_core")
    ad = FakeAdapter({"ask_timeout_s": 0.05})
    tap = WeComTap(ad, "req1", "u1", "stream1")
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
        # 问题文本进入流（force flush）+ 待答登记
        assert ad.pending["u1"]["options"] == ["A", "B"]
        assert any("1. A" in b["stream"]["content"] for _, b in ad.client.responds)
        await asyncio.sleep(0.12)  # 超过 ask_timeout_s
        assert resumed and resumed[0][1]["skip"] is True
        assert "u1" not in ad.pending
    finally:
        shared._PENDING_RESUME.discard("sess1")
        shared._PENDING_KIND.pop("sess1", None)


# ---------------------------------------------------------------- 降级渲染


def test_widget_to_markdown():
    md = _widget_to_markdown({"kind": "stat_list", "data": {
        "title": "状态", "items": [
            {"label": "调研", "value": "完成", "status": "done"},
            {"label": "实现", "value": "进行中", "status": "running"},
        ]}})
    assert "状态" in md and "✅" in md and "⏳" in md
    assert _widget_to_markdown({"kind": "unknown"}) == ""


def test_tap_ignores_noise_frames():
    ad = FakeAdapter()
    tap = WeComTap(ad, "req1", "u1", "stream1")
    # thinking.delta/tool.* 已升级为 P1 进展区事件（见
    # test_tap_progress_section_streams_and_clears）；这里只覆盖纯噪音帧
    for ev in ("keepalive", "artifacts.changed", "run.bind",
               "preview.invalidate"):
        asyncio.run(tap.send_text(_frame(ev, content="x", name="bash")))
    assert ad.client.responds == [] and ad.client.pushes == []


# ---------------------------------------------------------------- client


class _FakeWS:
    """假 websocket：send 订阅帧后异步回 ack（模拟服务端，覆盖 recv 先行的
    时序——回归 2026-10-28 订阅死锁：recv 循环晚于 ack 等待则超时）。"""

    def __init__(self, subscribe_errcode: int = 0):
        self._subscribe_errcode = subscribe_errcode
        self._incoming: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        frame = json.loads(payload)
        if frame["cmd"] == p.CMD_SUBSCRIBE:
            ack = {"headers": {"req_id": frame["headers"]["req_id"]},
                   "errcode": self._subscribe_errcode,
                   "errmsg": "ok" if self._subscribe_errcode == 0 else "bad secret"}
            self._incoming.put_nowait(json.dumps(ack))

    async def recv(self) -> str:
        return await self._incoming.get()

    async def close(self) -> None:
        pass


async def test_client_subscribe_ack_correlation(monkeypatch):
    import ginno_runtime.channels.wecom.client as client_mod

    async def fake_connect(*a, **kw):
        return _FakeWS()

    monkeypatch.setattr("websockets.connect", fake_connect)
    client = client_mod.WecomClient("bot", "sec")
    await client.connect_subscribe()
    assert client._recv_task is not None
    await client.close()


async def test_client_subscribe_rejects_credentials(monkeypatch):
    import ginno_runtime.channels.wecom.client as client_mod

    async def fake_connect(*a, **kw):
        return _FakeWS(subscribe_errcode=40001)

    monkeypatch.setattr("websockets.connect", fake_connect)
    client = client_mod.WecomClient("bot", "bad")
    with pytest.raises(client_mod.WecomAuthError):
        await client.connect_subscribe()


# ---------------------------------------------------------------- adapter 路由


def _make_adapter(tmp_path: Path, config: dict | None = None):
    from ginno_runtime.channels.wecom.adapter import WeComChannelAdapter
    from ginno_runtime.channels.wecom.sessions import SessionMapper

    ad = WeComChannelAdapter()
    ad.config = config or {}
    ad.client = FakeClient()
    mapper = SessionMapper(tmp_path / "m.json")
    mapper.load()
    ad._mapper = mapper
    return ad


def _msg_body(text: str = "你好", *, chattype: str = "single", **extra) -> dict:
    body = {
        "msgid": "m1", "msgtype": "text", "chattype": chattype,
        "from": {"userid": "zhang"}, "text": {"content": text},
    }
    body.update(extra)
    return body


def _frame_of(req_id: str = "req1") -> dict:
    return {"cmd": p.CMD_MSG_CALLBACK, "headers": {"req_id": req_id}, "body": {}}


def test_adapter_routes_text_to_turn(tmp_path: Path, monkeypatch):
    pytest.importorskip("langchain_core")
    ad = _make_adapter(tmp_path, {"session_policy": "per_user"})
    turns: list[dict] = []

    async def fake_ensure(ginno_sid, *, default_agent_id=None, title=None,
                          channel=None):
        return ({"session_id": "sess1", "project_slug": "default",
                 "graph": None}, None)

    async def fake_run(session, text, tap, **kw):
        turns.append({"session": session, "text": text, "tap": tap, **kw})
        tap.session_id = session["session_id"]

    import ginno_runtime.channels.wecom.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "ensure_channel_session", fake_ensure)
    monkeypatch.setattr(adapter_mod, "run_channel_turn", fake_run)
    asyncio.run(ad.handle_msg(_msg_body("@Ginno 查下天气"), _frame_of()))
    assert turns and turns[0]["text"] == "查下天气"
    assert turns[0]["usage_source"] == "wecom"
    # 占位首帧已发（5 秒时限路径）
    assert ad.client.responds and ad.client.responds[0][1]["msgtype"] == "stream"


async def test_adapter_busy_lock_finishes_placeholder(tmp_path: Path, monkeypatch):
    pytest.importorskip("langchain_core")
    ad = _make_adapter(tmp_path, {})
    lock = asyncio.Lock()

    async def locked():
        async with lock:
            await asyncio.sleep(0.2)

    async def fake_ensure(ginno_sid, *, default_agent_id=None, title=None,
                          channel=None):
        return ({"session_id": "sess1", "project_slug": "default"}, None)

    import ginno_runtime.channels.wecom.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "ensure_channel_session", fake_ensure)
    holder = asyncio.create_task(locked())
    await asyncio.sleep(0.02)
    ad._session_locks["sess1"] = lock
    await ad.handle_msg(_msg_body(), _frame_of())
    finishes = [b for _, b in ad.client.responds
                if b.get("stream", {}).get("finish")]
    assert finishes and "上一轮仍在进行中" in finishes[0]["stream"]["content"]
    await holder


async def test_adapter_busy_running_turn_steers(tmp_path: Path, monkeypatch):
    """在途（RUNNING_TURNS 有、未停车）时新消息 → steer 注入而非提示：
    与桌面 composer 同一队列（steer_enqueue），IM 侧收「已追加」回执。"""
    pytest.importorskip("langchain_core")
    from ginno_runtime import server_shared as shared

    ad = _make_adapter(tmp_path, {})
    lock = asyncio.Lock()

    async def locked():
        async with lock:
            await asyncio.sleep(0.2)

    async def fake_ensure(ginno_sid, *, default_agent_id=None, title=None,
                          channel=None):
        return ({"session_id": "sess1", "project_slug": "default"}, None)

    import ginno_runtime.channels.wecom.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "ensure_channel_session", fake_ensure)
    # 活 turn：占用锁 + RUNNING_TURNS 登记（run_channel_turn 的早武装）
    holder = asyncio.create_task(locked())
    await asyncio.sleep(0.02)
    ad._session_locks["sess1"] = lock
    monkeypatch.setitem(shared._RUNNING_TURNS, "sess1", "turn-live")
    await ad.handle_msg(_msg_body("@Ginno 顺便也看下Y"), _frame_of())
    finishes = [b for _, b in ad.client.responds
                if b.get("stream", {}).get("finish")]
    assert finishes and "已追加到当前任务" in finishes[0]["stream"]["content"]
    drained = shared.steer_drain("sess1")
    assert len(drained) == 1
    assert drained[0]["text"] == "顺便也看下Y"
    assert drained[0]["turn_id"] == "turn-live"
    assert drained[0]["origin"] == "channel"
    assert drained[0]["steer_id"] and drained[0]["steer_id"] != "turn-live"
    await holder


async def test_adapter_salvage_redelivers_as_push_turn(tmp_path: Path, monkeypatch):
    """run_channel_turn 返回未吸收追加 → 主动告知 + push_only tap 转新 turn
    （结果走 send_markdown 主动推送，第二级不再递归）。"""
    pytest.importorskip("langchain_core")
    ad = _make_adapter(tmp_path, {})
    calls: list[dict] = []

    async def fake_run(session, text, tap, **kw):
        calls.append({"text": text, "tap": tap,
                      "usage_source": kw.get("usage_source")})
        # 第一次（消息 turn）返回未吸收追加；补投 turn 返回空
        return ["哦不 讲10个"] if kw.get("usage_source") == "wecom" else []

    async def fake_ensure(ginno_sid, *, default_agent_id=None, title=None,
                          channel=None):
        return ({"session_id": "sess1", "project_slug": "default"}, None)

    import ginno_runtime.channels.wecom.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "ensure_channel_session", fake_ensure)
    monkeypatch.setattr(adapter_mod, "run_channel_turn", fake_run)
    await ad.handle_msg(_msg_body("@Ginno 讲5个笑话"), _frame_of())
    assert len(calls) == 2
    assert calls[0]["usage_source"] == "wecom"
    assert calls[1]["usage_source"] == "wecom_salvage"
    assert calls[1]["text"] == "哦不 讲10个"
    from ginno_runtime.channels.wecom.tap import WeComTap
    assert isinstance(calls[1]["tap"], WeComTap)
    assert calls[1]["tap"]._stream_done is True  # push_only：全程主动推送
    pushes = [c for _, c in ad.client.pushes]
    assert any("已自动转为新任务" in str(c) for c in pushes)


async def test_adapter_media_image_injects_multimodal(tmp_path: Path, monkeypatch):
    """入站图片：落盘 attached_files（工具路径）+ images 多模态载荷（composer
    同款，模型直接看到像素）。非图片格式回落 None（纯附件行为）。"""
    pytest.importorskip("langchain_core")
    from ginno_runtime.channels.base import image_payload_from_bytes
    from ginno_runtime.channels.wecom import media as media_mod

    ad = _make_adapter(tmp_path, {})
    got: list[dict] = []

    async def fake_run(session, text, tap, **kw):
        got.append({"text": text, "attached": kw.get("attached_files"),
                    "images": kw.get("images")})
        return []

    async def fake_ensure(ginno_sid, *, default_agent_id=None, title=None,
                          channel=None):
        return ({"session_id": "sess1", "project_slug": "default"}, None)

    async def fake_download(url, aeskey, http):
        return b"\x89PNG-fake", "shot.png"

    import ginno_runtime.channels.wecom.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "ensure_channel_session", fake_ensure)
    monkeypatch.setattr(adapter_mod, "run_channel_turn", fake_run)
    monkeypatch.setattr(media_mod, "download_media", fake_download)
    monkeypatch.setattr(adapter_mod.paths, "session_files_dir",
                        lambda slug, sid: tmp_path / "sf")
    await ad._dispatch_media(
        "req9", "ck1", "u1", {"msgid": "img1", "image": {"url": "https://x"}},
        "image")
    assert got and got[0]["attached"]
    assert got[0]["attached"][0]["kind"] == "image"
    assert got[0]["images"] and got[0]["images"][0]["media_type"] == "image/png"
    assert got[0]["images"][0]["data"] == base64.b64encode(b"\x89PNG-fake").decode()
    # helper：不可识别扩展名 / 超限 → None（回落纯 attached_files）
    assert image_payload_from_bytes(b"x", "a.bin") is None
    assert image_payload_from_bytes(b"x" * (8 * 1024 * 1024 + 1), "a.png") is None
    # 魔数嗅探优先：.png 名下的 JPEG 字节按 image/jpeg 上报（vision API
    # 会校验 mime 与字节一致）
    assert image_payload_from_bytes(b"\xff\xd8\xff\xe0jpg", "a.png")[0][
        "media_type"] == "image/jpeg"
    assert image_payload_from_bytes(b"\x89PNG-rest", "noext")[0][
        "media_type"] == "image/png"


def test_adapter_pending_question_short_circuits(tmp_path: Path, monkeypatch):
    pytest.importorskip("langchain_core")
    ad = _make_adapter(tmp_path, {})
    ad._pending_q["zhang"] = {"session_id": "sess1", "question_id": "q1",
                              "options": ["A", "B"]}
    resumed: list[tuple[str, dict]] = []

    import ginno_runtime.server_shared as shared

    shared._PENDING_RESUME.add("sess1")
    shared._PENDING_KIND["sess1"] = "user_question"

    import ginno_runtime.api.stream.resume as resume_mod

    monkeypatch.setattr(resume_mod, "_spawn_resume_task",
                        lambda sid, val, cfg: resumed.append((sid, val)))
    try:
        asyncio.run(ad.handle_msg(_msg_body("2"), _frame_of()))
    finally:
        shared._PENDING_RESUME.discard("sess1")
        shared._PENDING_KIND.pop("sess1", None)
    assert resumed and resumed[0][0] == "sess1"
    assert resumed[0][1]["option_index"] == 1
    assert ad.client.responds == []  # 作答不开新 turn，也不发占位帧


def test_adapter_whitelist_and_channel_gate(tmp_path: Path):
    ad = _make_adapter(tmp_path, {"allowed_userids": ["li si"]})
    asyncio.run(ad.handle_msg(_msg_body(), _frame_of()))  # zhang 不在白名单
    assert ad.client.responds == []
    # 频道消息未被 @ → 忽略
    ad2 = _make_adapter(tmp_path, {})
    body = _msg_body("今天天气如何", chattype="group", chatid="wr1",
                     compat_info={"source": "channel"})
    asyncio.run(ad2.handle_msg(body, _frame_of()))
    assert ad2.client.responds == []


def test_adapter_enter_chat_welcome(tmp_path: Path):
    ad = _make_adapter(tmp_path, {"welcome_text": "你好呀"})
    frame = {"headers": {"req_id": "req9"}, "cmd": p.CMD_EVENT_CALLBACK}
    asyncio.run(ad.handle_event({"event": {"eventtype": "enter_chat"}}, frame))
    assert ad.client.responds and ad.client.responds[0][1]["text"]["content"] == "你好呀"
