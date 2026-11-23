"""A worker result can park a WeCom-bound session without a channel tap."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import events_of, script, script_tool_call

from ginno_runtime import jsonx as json
from ginno_runtime import server_shared
from ginno_runtime.channels.wecom.adapter import WeComChannelAdapter
from ginno_runtime.channels.wecom.sessions import SessionMapper
from ginno_runtime.connectors import builtin
from ginno_runtime.session_meta import mark_channel_meta
from ginno_runtime.subagent_scheduler.runner import _wake_parent_turn

pytestmark = pytest.mark.e2e


def test_wake_turn_sends_once_and_wecom_answer_resumes_same_question(
    client, create_session, ws_conv, isolated_home, monkeypatch,
):
    sid = create_session([
        script(tool_calls=[script_tool_call(
            "ask_user",
            {"header": "验收选择", "question": "选哪个？", "options": ["A", "B"]},
            "call_wecom_question",
        )]),
        script(text="收到选择。"),
    ])
    settings = isolated_home / "settings.json"
    raw = json.loads(settings.read_text())
    raw["ask_relay_wecom"] = True
    settings.write_text(json.dumps(raw))
    ad = WeComChannelAdapter()
    ad._mapper = SessionMapper(isolated_home / "channels" / "test-mappings.json")
    ad._mapper.bind(user_id="bound-user", chat_id="bound-user", ginno_session=sid)
    ad.last_chat_key = "unrelated-user"
    ad.client = AsyncMock()
    monkeypatch.setattr(builtin, "_wecom_adapter", ad)
    client.portal.call(mark_channel_meta, server_shared._SESSIONS[sid], "wecom")

    # The fake replaces only the external network request. The graph, event
    # dispatch, pending-answer registration and adapter callbacks are real.
    async def send_answer():
        assert ad.client.send_markdown.await_count == 1
        chat, card = ad.client.send_markdown.await_args.args
        assert chat == "bound-user"
        assert "验收选择" in card and "1. A" in card and "2. B" in card
        await ad.handle_msg(
            {"from": {"userid": chat}, "chattype": "single", "msgtype": "text",
             "text": {"content": "2"}},
            {"headers": {"req_id": "test-answer"}},
        )
        assert ad.client.respond.await_count == 0  # no new channel turn

    with ws_conv(sid) as conv:
        assert client.portal.call(_wake_parent_turn, sid, "子代理结果", {}) == "ran"
        parked = conv.recv_until("user.question", "error", "message.end")
        assert events_of(parked, "user.question")[0]["id"] == "call_wecom_question"
        # The managed turn has already settled at its interrupt before answering.
        client.portal.call(send_answer)
        resumed = conv.recv_until("message.end", "error")
        assert not events_of(resumed, "error")

    results = [e for e in events_of(resumed, "tool.end") if e.get("id") == "call_wecom_question"]
    assert len(results) == 1
    receipt = json.loads(results[0]["content"])
    assert receipt["source"] == "option"
    assert receipt["answer"] == "B" and receipt["option_index"] == 1
