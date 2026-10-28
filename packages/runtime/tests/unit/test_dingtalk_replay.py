"""L2 数据入口回放：构造钉钉消息 dict 直喂 adapter，端到端跑通
F2 路由 → headless turn（fake 引擎按剧本推帧）→ tap 翻译 → F4 按钮回调 →
真实 resume_pending_interrupt 校验受理。

不连 SDK、不连网络：``api.stream`` 以 stub 模块替换（fake ``_run_stream``
按剧本向 ``_SESSION_WS`` 推帧——这正是真引擎做的事）；``ensure_channel_session``
打桩返回 fake session。resume 公共入口是**真实实现**（peek 校验/park 清理），
仅最底层 ``_spawn_resume_task`` 被替换为记录器（真 graph resume 属 L3 联调）。
"""

from __future__ import annotations

import asyncio
import sys
import types

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import server_shared as shared
from ginno_runtime.channels import base as channels_base
from ginno_runtime.channels.dingtalk import menus as menus_mod
from ginno_runtime.channels.dingtalk.adapter import DingTalkChannelAdapter

# adapter 顶层依赖 server_shared → checkpointer → langgraph：本文件定位为
# 完整 venv 层（L2），轻环境自动跳过（L0/L1 已覆盖协议与翻译层）。
pytest.importorskip("langgraph", reason="L2 replay needs full runtime venv")

# ---------------------------------------------------------------- fakes


class FakeApi:
    """记录单发请求；附件下载走 MockTransport（协议已由 L1 覆盖）。"""

    def __init__(self, recorder: Recorder) -> None:
        self.rec = recorder
        self._mock = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    @staticmethod
    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "tk", "expireIn": 7200})
        if p == "/v1.0/robot/messageFiles/download":
            return httpx.Response(200, json={"downloadUrl": "https://mock.local/f"})
        if request.url.host == "mock.local":
            return httpx.Response(200, content=b"XLSX-BYTES")
        return httpx.Response(200, json={"processQueryKey": "k"})

    async def send_single_message(self, staff_id: str, msg_key: str, msg_param: dict) -> dict:
        self.rec.sends.append((staff_id, msg_key, msg_param))
        return {"processQueryKey": "k"}

    async def download_message_file(self, code: str, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._mock.stream("GET", "https://mock.local/f") as r:
            dest.write_bytes(await r.aread())
        return dest

    async def upload_media(self, path, *, type: str = "file") -> str:
        return "m-1"

    async def close(self) -> None:
        await self._mock.aclose()


class FakeCards:
    def __init__(self, recorder: Recorder) -> None:
        self.rec = recorder
        self._card = types.SimpleNamespace(out_track_id="ot-1", staff_id="")

    async def create_stream_card(self, staff_id, placeholder="…"):
        self.rec.card_events.append(("create", staff_id))
        self._card.staff_id = staff_id
        return self._card

    async def stream_update(self, card, md):
        self.rec.card_events.append(("stream", md))

    async def finalize(self, card, md, status="done"):
        self.rec.card_events.append(("finalize", status, md))

    async def send_question_card(self, staff_id, q):
        self.rec.questions.append(dict(q))
        return str(q["id"])

    async def disable_question_card(self, qid):
        self.rec.card_events.append(("disabled", qid))


class Recorder:
    def __init__(self) -> None:
        self.sends: list[tuple] = []
        self.card_events: list[tuple] = []
        self.questions: list[dict] = []
        self.turns: list[dict] = []          # 每次 fake _run_stream 的 config/text
        self.resumes: list[tuple[str, dict]] = []


def _bot_text(staff: str, text: str, cid: str = "cid-1") -> dict:
    return {"conversationId": cid, "senderStaffId": staff, "senderNick": "测试者",
            "msgtype": "text", "text": {"content": text},
            "sessionWebhook": "https://mock.local/hook"}


def _make_adapter(monkeypatch, rec: Recorder, *, config: dict | None = None,
                  script: str = "echo", files_dir=None) -> DingTalkChannelAdapter:
    """装配 adapter：不 start()（不拉 SDK），手工注入 api/cards/映射/菜单。"""

    from ginno_runtime.channels.dingtalk.sessions import SessionMapper

    a = DingTalkChannelAdapter()
    a.config = config or {"enabled": True, "ask_timeout_s": 600,
                          "allowed_staff_ids": ["s-allow"]}
    a.api = FakeApi(rec)
    a.cards = FakeCards(rec)
    a._menus = menus_mod.load_menus({"menus": [
        {"trigger": "daily", "title": "日报", "prompt": "生成 {date} 日报"}]})
    a._mapper = SessionMapper(_tmp_store())
    a._mapper.load()

    # ① fake turn 引擎：stub 整个 api.stream 包（真引擎向 _SESSION_WS 推帧，
    #    fake 按剧本做同样的事；run_channel_turn 侧的挂载/清理是真实代码）
    resume_evt = asyncio.Event()

    async def fake_run_stream(ws, graph, config, text, session, agent_id,
                              images=None, skill_name=None) -> None:
        sid = config["configurable"]["thread_id"]
        rec.turns.append({"config": config, "text": text})
        await _emit(sid, "turn.start")
        if script == "echo":
            await _emit(sid, "token.delta", content=f"ECHO:{text}")
            await _emit(sid, "message.end", text="…")
        elif script == "ask":
            await _emit(sid, "token.delta", content="先问你：")
            shared._PENDING_RESUME.add(sid)
            shared._PENDING_KIND[sid] = "user_question"
            await _emit(sid, "user.question", id="q-1", question="选哪个？",
                        header="确认", options=["A", "B"], allow_free_text=True)
            await resume_evt.wait()          # 真实引擎在 interrupt 处暂停
            await _emit(sid, "token.delta", content="收到答案，继续。")
            await _emit(sid, "message.end", text="…")

    async def _emit(sid: str, ev: str, **data) -> None:
        for s in list(shared._SESSION_WS.get(sid, [])):
            await s.send_text(json.dumps({"event": ev, **data}, ensure_ascii=False))

    stub = types.ModuleType("ginno_runtime.api.stream")
    stub._run_stream = fake_run_stream
    # 让 `import ginno_runtime.api.stream.resume`（tap/handle_card_callback 的
    # lazy import）能经 stub 找到真实子模块文件。
    import ginno_runtime.api as _api_pkg
    import ginno_runtime.api.stream as _real_pkg

    stub.__path__ = _real_pkg.__path__
    monkeypatch.setitem(sys.modules, "ginno_runtime.api.stream", stub)
    # `from ..api import stream`（run_channel_turn 的懒加载）按 CPython 语义
    # 走**父包属性**查找，sys.modules 项拦不住——父包属性必须一起换，
    # 否则 fake 永远不生效、真实 _run_stream 拿假 session 直接炸。
    monkeypatch.setattr(_api_pkg, "stream", stub)
    a._resume_evt = resume_evt

    # ② fake session（绕开 api.sessions 的重依赖；其内部路径属 L3）。
    # patch 目标必须是 adapter 模块：adapter 顶部 `from ..base import
    # ensure_channel_session` 已把名字绑进自己命名空间，只 patch base 的
    # 属性拦不住——真实路径会去读用户 settings（provider 关了就全线炸）。
    # 签名跟 base.ensure_channel_session 对齐（title/channel）。
    from ginno_runtime.channels.dingtalk import adapter as adapter_mod

    async def fake_ensure(session_id, *, default_agent_id=None, title=None,
                          channel=None):
        sid = session_id or "sess-fresh"
        fake_session = {"session_id": sid, "project_slug": "default",
                        "agent_id": default_agent_id or "main", "graph": object()}
        return fake_session, sid

    monkeypatch.setattr(adapter_mod, "ensure_channel_session", fake_ensure)

    # 附件落盘隔离到 tmp（不写真实 ~/.ginno 会话目录）
    if files_dir is not None:
        monkeypatch.setattr(
            adapter_mod.paths, "session_files_dir", lambda slug, sid: files_dir)
    return a


def _tmp_store(name: str = "replay_mappings.json"):
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp(prefix="ginno-l2-"))
    return d / name


def _patch_spawn(monkeypatch, rec: Recorder, resume_evt: asyncio.Event) -> None:
    """resume_pending_interrupt 用真实实现；仅替换最底层 spawn（记录+放行剧本）。"""
    import ginno_runtime.api.stream.resume as resume_mod

    def fake_spawn(session_id: str, resume_value: dict, base_config=None) -> None:
        rec.resumes.append((session_id, resume_value))
        resume_evt.set()

    monkeypatch.setattr(resume_mod, "_spawn_resume_task", fake_spawn)


# ---------------------------------------------------------------- 用例

async def test_e2e_text_reply_streams_to_card(monkeypatch):
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec)
    await a.handle_bot_message(_bot_text("s-allow", "你好"))
    assert len(rec.turns) == 1
    cfg = rec.turns[0]["config"]["configurable"]
    assert cfg["usage_source"] == "dingtalk" and cfg["user_text"] == "你好"
    # 渠道轮次带 echo_user：engine 的 turn.start 据此回传用户原文，
    # 桌面端渠道会话才能实时回显 IM 侧输入（无 composer 乐观气泡）。
    assert cfg["echo_user"] is True
    assert rec.card_events[0][0] == "create"
    assert any(e[0] == "stream" and "ECHO:你好" in e[1] for e in rec.card_events)
    assert rec.card_events[-1][0] == "finalize" and rec.card_events[-1][1] == "done"
    # 映射已绑定：下一条消息复用同一 session
    await a.handle_bot_message(_bot_text("s-allow", "再会"))
    assert rec.turns[1]["config"]["configurable"]["thread_id"] == \
        rec.turns[0]["config"]["configurable"]["thread_id"]


async def test_e2e_ask_user_button_resume_full_chain(monkeypatch):
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec, script="ask")
    _patch_spawn(monkeypatch, rec, a._resume_evt)   # resume 放行剧本等待

    async def answer_later() -> None:
        await asyncio.sleep(0.1)             # 等 tap 发出问题卡片并登记
        await a.handle_card_callback(
            {"content": json.dumps({"q": "q-1", "act": "opt", "i": 1})})

    bot = asyncio.create_task(a.handle_bot_message(_bot_text("s-allow", "触发提问")))
    await answer_later()
    await bot
    # 问题卡片按协议投放
    assert rec.questions and rec.questions[0]["options"] == ["A", "B"]
    # 按钮回调 → 真实 resume_pending_interrupt 受理（校验停车种类并清理）
    assert len(rec.resumes) == 1
    sid, val = rec.resumes[0]
    assert val == {"kind": "user_answer", "answer": "", "option_index": 1, "skip": False}
    assert sid not in shared._PENDING_RESUME and shared._PENDING_KIND.get(sid) is None
    # 答后卡片置灰 + turn 剧本走完（收到答案 → message.end → finalize）
    assert any(e[0] == "disabled" and e[1] == "q-1" for e in rec.card_events)
    assert rec.card_events[-1][0] == "finalize"
    assert any(e[0] == "stream" and "收到答案" in e[1] for e in rec.card_events)


async def test_e2e_unauthorized_silently_dropped(monkeypatch):
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec)
    await a.handle_bot_message(_bot_text("s-stranger", "你好"))
    assert rec.turns == [] and rec.sends == [] and rec.card_events == []


async def test_e2e_menu_help_and_command(monkeypatch):
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec)
    await a.handle_bot_message(_bot_text("s-allow", "/help"))
    assert rec.turns == []
    assert any(k == "sampleMarkdown" and "日报" in str(p) for _, k, p in rec.sends)
    await a.handle_bot_message(_bot_text("s-allow", "/daily"))
    assert len(rec.turns) == 1
    assert rec.turns[0]["text"].startswith("生成 ") and "日报" in rec.turns[0]["text"]
    assert rec.turns[0]["config"]["configurable"]["usage_source"] == "dingtalk_menu"


async def test_e2e_busy_when_turn_inflight(monkeypatch):
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec, script="ask")
    _patch_spawn(monkeypatch, rec, a._resume_evt)
    bot = asyncio.create_task(a.handle_bot_message(_bot_text("s-allow", "触发提问")))
    await asyncio.sleep(0.15)                # turn 已停车等答案（lock 仍被持有）
    await a.handle_bot_message(_bot_text("s-allow", "插队消息"))
    assert len(rec.turns) == 1               # 未开新 turn
    assert any("进行中" in str(p) for _, k, p in rec.sends if k == "sampleMarkdown")
    await a.handle_card_callback(
        {"content": json.dumps({"q": "q-1", "act": "skip", "i": -1})})
    await bot
    assert rec.resumes[0][1]["skip"] is True


async def test_e2e_clear_command_short_circuits(monkeypatch):
    """/clear 走 builtin 短路（与桌面 WS invoke 同口径）：不驱动 turn，回执
    经 tap 翻译下行（真机 2026-10-28：曾当纯文本喂给模型，上下文没清）。"""
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec)
    await a.handle_bot_message(_bot_text("s-allow", "/clear"))
    assert len(rec.turns) == 0               # 没有开 LLM turn
    # fake session 的 graph 是 object()：aget_state 抛错 → snap=None → 「上下
    # 文已经是空的」回执经 tap 翻译成钉钉卡片流；真实链路上有消息时此处执
    # 行清空 + history.cleared 广播（桌面实时丢转录）。
    assert any(e[0] == "create" for e in rec.card_events)
    assert any(e[0] in ("stream", "finalize") and ("已经是空" in str(e[1]) or "already empty" in str(e[1]))
               for e in rec.card_events)


async def test_e2e_running_turn_steers_instead_of_busy(monkeypatch):
    """在途且未停车（RUNNING_TURNS 有、非 PENDING_RESUME）→ 新消息 steer
    注入（与桌面 composer 同队列），IM 侧收「已追加」回执，不开新 turn。"""
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec)

    async def hold_lock():
        async with a._session_locks.setdefault("sess-fresh", asyncio.Lock()):
            await asyncio.sleep(0.2)

    holder = asyncio.create_task(hold_lock())
    await asyncio.sleep(0.02)
    monkeypatch.setitem(shared._RUNNING_TURNS, "sess-fresh", "turn-live")
    await a.handle_bot_message(_bot_text("s-allow", "顺便也看下Y"))
    assert len(rec.turns) == 0               # 未开新 turn
    assert any("已追加到当前任务" in str(p)
               for _, k, p in rec.sends if k == "sampleMarkdown")
    drained = shared.steer_drain("sess-fresh")
    assert len(drained) == 1
    assert drained[0]["text"] == "顺便也看下Y"
    assert drained[0]["turn_id"] == "turn-live"
    assert drained[0]["origin"] == "channel"
    assert drained[0]["steer_id"] and drained[0]["steer_id"] != "turn-live"
    await holder


async def test_e2e_steer_salvage_redelivers_as_followup_turn(monkeypatch):
    """turn 在最终 superstep 后收尾、追加未吸收 → run_channel_turn 排水返回
    → adapter 主动告知并转新 turn 补投（「已追加」不能是空头支票）。"""
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec)
    # 预置一条渠道来源 steer（模拟「enqueue 后 turn 收尾、engine 段末
    # steer_keep_channel 保留」的状态；fake 引擎不清场，等价）
    shared.steer_enqueue("sess-fresh", {
        "steer_id": "s-steer-1", "turn_id": "t-old", "text": "哦不 讲10个",
        "injected_at": 1.0, "origin": "channel",
        "content_blocks": None, "context_text": None, "files": [], "images": [],
    })
    await a.handle_bot_message(_bot_text("s-allow", "讲5个笑话"))
    # 原 turn + 补投 turn（第二次调度文本=追加原文，来源标记 dingtalk_salvage）
    assert len(rec.turns) == 2
    assert rec.turns[1]["text"] == "哦不 讲10个"
    assert rec.turns[1]["config"]["configurable"]["usage_source"] == "dingtalk_salvage"
    assert any("已自动转为新任务" in str(p)
               for _, k, p in rec.sends if k == "sampleMarkdown")
    # 排水后 stash 已清，无残留
    assert shared.steer_drain("sess-fresh") == []


async def test_e2e_attachment_downloads_and_attaches(monkeypatch, tmp_path):
    rec = Recorder()
    a = _make_adapter(monkeypatch, rec, files_dir=tmp_path)
    await a.handle_bot_message({
        "conversationId": "cid-1", "senderStaffId": "s-allow", "senderNick": "测试者",
        "msgtype": "file", "downloadCode": "dc-9", "fileName": "报表.xlsx",
    })
    assert len(rec.turns) == 1
    cfg = rec.turns[0]["config"]["configurable"]
    files = cfg.get("attached_files") or []
    assert len(files) == 1 and files[0]["name"] == "报表.xlsx"
    assert files[0]["path"].endswith("报表.xlsx") and files[0]["kind"] == "file"
    with open(files[0]["path"], "rb") as fh:  # 下载内容真实落盘
        assert fh.read() == b"XLSX-BYTES"
