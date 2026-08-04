"""E2E: context governance over the real WS flow — history compaction
(E3/E4) and tool-output truncation (E2)."""

from __future__ import annotations

import json

import pytest
from conftest import events_of, script, script_tool_call

from ginno_runtime.truncation import TRUNCATION_MARKER
from ginno_runtime.world_state import REINJECT_MSG_PREFIX, SUMMARY_MSG_PREFIX

pytestmark = pytest.mark.e2e


def _settings_context(home, **overrides):
    sp = home / "settings.json"
    s = json.loads(sp.read_text()) if sp.exists() else {}
    s.setdefault("context", {}).update(overrides)
    sp.write_text(json.dumps(s))


def _all_history(client, sid):
    return client.get(f"/api/sessions/{sid}/history").json().get("messages", [])


# --------------------------------------------------------------------------- #
# E3/E4 — compaction
# --------------------------------------------------------------------------- #
def test_compaction_fires_and_history_stays_usable(create_session, ws_conv, client, isolated_home):
    _settings_context(
        isolated_home,
        compaction_enabled=True,
        compact_threshold_tokens=30,  # tiny → fires on the 3rd turn
        compact_keep_turns=1,
    )
    sid = create_session(
        [
            script(text="第一轮回答。" * 5),
            script(text="第二轮回答。" * 5),
            script(text="这是对话摘要：用户问了两轮。"),  # summarizer call
            script(text="第三轮回答。"),
        ],
        agent_id="dev",
    )

    with ws_conv(sid) as conv:
        conv.invoke("第一个问题")
        conv.recv_until("message.end", "error")
        conv.invoke("第二个问题")
        conv.recv_until("message.end", "error")
        conv.invoke("第三个问题")
        events = conv.recv_until("message.end", "error")

    compacted = events_of(events, "context.compacted")
    assert len(compacted) == 1, "compaction should announce itself on turn 3"
    assert compacted[0]["compacted_messages"] >= 2

    entries = _all_history(client, sid)
    texts = json.dumps(entries, ensure_ascii=False)
    # summary + world re-injection are system context rows in the transcript
    assert SUMMARY_MSG_PREFIX in texts
    assert REINJECT_MSG_PREFIX in texts
    assert "这是对话摘要" in texts
    # the summarized-away first question is gone; the kept tail remains
    assert "第一个问题" not in texts
    assert "第三个问题" in texts
    # E4: re-injection still carries the world facts
    assert "<environment>" in texts


def test_compaction_disabled_does_nothing(create_session, ws_conv, client, isolated_home):
    _settings_context(isolated_home, compaction_enabled=False, compact_threshold_tokens=1)
    sid = create_session(
        [script(text="a1"), script(text="a2")], agent_id="dev"
    )
    with ws_conv(sid) as conv:
        conv.invoke("q1")
        conv.recv_until("message.end", "error")
        conv.invoke("q2")
        events = conv.recv_until("message.end", "error")
    assert events_of(events, "context.compacted") == []
    assert SUMMARY_MSG_PREFIX not in json.dumps(_all_history(client, sid), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# E2 — tool output truncation in history
# --------------------------------------------------------------------------- #
def test_big_tool_output_truncated_in_history(create_session, ws_conv, client, isolated_home):
    _settings_context(isolated_home, tool_output_max_chars=2000)
    ws_dir = isolated_home / "ws"
    ws_dir.mkdir(exist_ok=True)
    big = ws_dir / "big.txt"
    big.write_text("HEAD-MARK\n" + "x" * 8000 + "\nTAIL-MARK\n", encoding="utf-8")

    sid = create_session(
        [
            script(
                tool_calls=[
                    script_tool_call("read_file", {"path": str(big)}),
                ]
            ),
            script(text="读完了。"),
        ],
        agent_id="dev",
        workspace=str(ws_dir),
    )
    with ws_conv(sid) as conv:
        conv.invoke("读一下这个文件")
        events = conv.recv_until("message.end", "error")
    assert events_of(events, "message.end")

    texts = json.dumps(_all_history(client, sid), ensure_ascii=False)
    assert TRUNCATION_MARKER in texts
    assert "HEAD-MARK" in texts  # head kept
    assert "TAIL-MARK" in texts  # tail kept
    assert "x" * 4000 not in texts  # middle dropped


def test_small_tool_output_untouched(create_session, ws_conv, client, isolated_home):
    ws_dir = isolated_home / "ws"
    ws_dir.mkdir(exist_ok=True)
    small = ws_dir / "small.txt"
    small.write_text("just a line", encoding="utf-8")
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("read_file", {"path": str(small)})]),
            script(text="ok"),
        ],
        agent_id="dev",
        workspace=str(ws_dir),
    )
    with ws_conv(sid) as conv:
        conv.invoke("read it")
        conv.recv_until("message.end", "error")
    texts = json.dumps(_all_history(client, sid), ensure_ascii=False)
    assert "just a line" in texts
    assert TRUNCATION_MARKER not in texts
