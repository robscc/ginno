"""ask_relay（2026-11-21 企微异步人审）单测。"""

from __future__ import annotations

import asyncio
import contextvars
from unittest.mock import AsyncMock

import pytest

from ginno_runtime import server_shared
from ginno_runtime.channels import ask_relay
from ginno_runtime.channels.wecom.adapter import WeComChannelAdapter
from ginno_runtime.channels.wecom.tap import WeComTap


def test_parse_answer_digit_substring_free(monkeypatch):
    opts = ["安装到全局", "装到项目", "取消"]
    assert ask_relay._parse_answer("1", opts) == "安装到全局"
    assert ask_relay._parse_answer("全局", opts) == "安装到全局"
    assert ask_relay._parse_answer("随便说说", opts) == "随便说说"  # 自由文本
    assert ask_relay._parse_answer("9", opts) is None  # 越界数字不算
    assert ask_relay._parse_answer("", opts) is None


def test_try_answer_unattended_roundtrip():
    ev = asyncio.Event()
    ask_relay._UNATTENDED_Q["ck1"] = {
        "event": ev, "answer": None, "options": ["a方案", "b方案"], "t0": 0
    }
    # 命中选项
    assert ask_relay.try_answer_unattended("ck1", "1") is True
    assert ev.is_set()
    assert ask_relay._UNATTENDED_Q["ck1"]["answer"] == "a方案"
    # 已答后再来消息：不消化（slot 已 set）
    assert ask_relay.try_answer_unattended("ck1", "2") is False
    ask_relay._UNATTENDED_Q.clear()
    # 无在等问题
    assert ask_relay.try_answer_unattended("ck-none", "1") is False


@pytest.mark.asyncio
async def test_relay_ask_timeout_fallback(monkeypatch):
    """无 wecom adapter → None（fail-safe，回退自选默认）。"""
    monkeypatch.setattr(ask_relay, "_wecom_adapter", lambda: None)
    assert await ask_relay.relay_ask("q", ["a", "b"], "h", 0.05) is None


@pytest.mark.asyncio
async def test_relay_ask_answered(monkeypatch):
    """推送成功 + 企微作答 → 返回答案。"""
    sent: list[str] = []

    class _Client:
        async def send_markdown(self, chat_key, md):
            sent.append(md)

    class _Ad:
        client = _Client()
        last_chat_key = "ck-relay"

    monkeypatch.setattr(ask_relay, "_wecom_adapter", lambda: _Ad())

    async def _answer_later():
        await asyncio.sleep(0.02)
        assert ask_relay.try_answer_unattended("ck-relay", "b方案") is True

    task = asyncio.create_task(_answer_later())
    ans = await ask_relay.relay_ask("选哪个?", ["a方案", "b方案"], "测", 5)
    await task
    assert ans == "b方案"
    assert any("选哪个?" in s for s in sent)
    assert not ask_relay._UNATTENDED_Q  # finally 清场


@pytest.fixture
def mirror_adapter(monkeypatch):
    ad = WeComChannelAdapter()
    ad.client = AsyncMock()
    ad.last_chat_key = "chat"
    monkeypatch.setattr(ask_relay, "_wecom_adapter", lambda: ad)
    return ad


@pytest.mark.asyncio
async def test_mirror_pushes_question_and_registers_original_session(mirror_adapter):
    data = {"id": "call-1", "qid": "q-1", "question": "选哪个？", "options": ["A", "B"]}
    await ask_relay._mirror_question("s1", data)
    mirror_adapter.client.send_markdown.assert_awaited_once()
    chat, card = mirror_adapter.client.send_markdown.await_args.args
    assert chat == "chat" and "1. A" in card and "2. B" in card
    assert mirror_adapter._pending_q[chat]["session_id"] == "s1"
    assert mirror_adapter._pending_q[chat]["question_id"] == "q-1"


async def test_retired_mirror_releases_only_the_synced_question(mirror_adapter):
    synced = await mirror_adapter.register_pending_question("chat", "main", "q_sync", ["A", "B"])
    latest = await mirror_adapter.register_pending_question("other", "main", "q_new", ["X", "Y"])
    unrelated = await mirror_adapter.register_pending_question("worker", "worker", "q_sync", ["A", "B"])

    await ask_relay.retire_mirrored_question("main", "q_sync")

    assert not await mirror_adapter.unregister_pending_question("chat", synced)
    assert mirror_adapter._pending_q == {"other": latest, "worker": unrelated}
    mirror_adapter.client.send_markdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_mirror_skips_wecom_tap_and_previously_sent_question(mirror_adapter, monkeypatch):
    tap = WeComTap(mirror_adapter, "req", "chat", "stream")
    tap.session_id = "s1"
    monkeypatch.setitem(server_shared._SESSION_WS, "s1", [tap])
    data = {"id": "call-1", "qid": "q-1", "question": "选哪个？", "options": ["A", "B"]}

    await ask_relay._mirror_question("s1", data)
    mirror_adapter.client.send_markdown.assert_not_awaited()
    await tap.on_frame("user.question", data)
    mirror_adapter.client.respond.assert_awaited_once()
    server_shared._SESSION_WS["s1"].clear()

    await ask_relay._mirror_question("s1", data)
    mirror_adapter.client.send_markdown.assert_not_awaited()

    monkeypatch.setitem(server_shared._PENDING_KIND, "s1", "user_question")
    await tap.close()
    assert mirror_adapter._pending_q["chat"]["question_id"] == "q-1"
    await tap._cancel_ask_timer()


@pytest.mark.asyncio
async def test_mirror_preserves_other_session_waiting_for_same_chat(mirror_adapter):
    data = {"id": "call-1", "qid": "q-1", "question": "选哪个？"}
    server_shared._PENDING_RESUME.add("s1")
    try:
        await ask_relay._mirror_question("s1", data)
        first = mirror_adapter._pending_q["chat"]
        await ask_relay._mirror_question("s2", {**data, "qid": "q-2"})
        mirror_adapter.client.send_markdown.assert_awaited_once()
        assert mirror_adapter._pending_q["chat"] is first
    finally:
        server_shared._PENDING_RESUME.discard("s1")


@pytest.mark.asyncio
async def test_mirror_push_failure_rolls_back_and_allows_retry(mirror_adapter):
    data = {"id": "call-1", "qid": "q-1", "question": "选哪个？"}
    mirror_adapter.client.send_markdown.side_effect = [ConnectionError("offline"), {}]
    await ask_relay._mirror_question("s1", data)
    mirror_adapter.client.send_markdown.assert_awaited_once()
    assert "chat" not in mirror_adapter._pending_q

    await ask_relay._mirror_question("s1", data)
    assert mirror_adapter.client.send_markdown.await_count == 2
    assert mirror_adapter._pending_q["chat"]["session_id"] == "s1"


def test_ask_tools_unattended_relay_branch(monkeypatch):
    """无人值守分支：relay 有答案 → 注入；未开启 → 原自选默认回执。"""
    from ginno_runtime.tools.ask_tools import build_ask_tools

    (t,) = build_ask_tools(session_id="s-relay")

    # 未开启：原回执（含「自选默认」语义标识）
    monkeypatch.setattr(
        "ginno_runtime.tools.ask_tools._INTERACTIVE",
        contextvars.ContextVar("i", default=False),
    )
    out = t.func(question="继续吗?", options=["继续", "停"])
    assert "自主续跑" in out or "unattended" in out.lower()

    # 开启且企微答了：答案注入
    monkeypatch.setattr(
        "ginno_runtime.channels.ask_relay._relay_enabled", lambda: True
    )
    monkeypatch.setattr(
        "ginno_runtime.channels.ask_relay.relay_ask_sync",
        lambda q, o, h, tm: "继续",
    )
    out2 = t.func(question="继续吗?", options=["继续", "停"])
    assert "企业微信作答" in out2 and "继续" in out2
