"""ask_relay（2026-11-21 企微异步人审）单测。"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from ginno_runtime.channels import ask_relay


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


@pytest.mark.asyncio
async def test_mirror_skips_wecom_own_question(monkeypatch):
    """wecom 自己 tap 已发的 qid 不镜像（防双卡片）。"""
    class _Ad:
        client = object()
        last_chat_key = "ck"
        _own_question_ids = ["q-own"]

    monkeypatch.setattr(ask_relay, "_relay_enabled", lambda: True)
    monkeypatch.setattr(ask_relay, "_wecom_adapter", lambda: _Ad())
    pushed = []

    async def _fake_mirror(sid, data):
        pushed.append((sid, data))

    monkeypatch.setattr(ask_relay, "_mirror_question", _fake_mirror)
    # 非 question 事件：不触发
    ask_relay.on_turn_event("s1", "token.delta", {})
    assert pushed == []
    # wecom 自己的 qid：_mirror_question 内部去重——直接测 _mirror_question
    # 走真实路径（push_only 构造会失败？mock tap）
    import ginno_runtime.channels.wecom.tap as tap_mod

    orig = tap_mod.WeComTap.push_only
    tap_mod.WeComTap.push_only = classmethod(
        lambda cls, ad, ck: pytest.fail("不应为 wecom 自己的 qid 镜像")
    )
    try:
        await ask_relay._mirror_question("s1", {"id": "q-own", "question": "x"})
    finally:
        tap_mod.WeComTap.push_only = orig


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
