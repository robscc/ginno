"""E2E: persisted chat history is served in the UI block format (powers session switch-back)."""

from __future__ import annotations


import pytest

from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def test_history_empty_for_brand_new_session(client, create_session):
    sid = create_session([script(text="hi")])
    r = client.get(f"/api/sessions/{sid}/history").json()
    assert r["ok"] is True
    assert r["messages"] == []


def test_history_unknown_session_returns_empty(client):
    assert client.get("/api/sessions/does-not-exist/history").json() == {"ok": True, "messages": []}


def test_history_after_text_turn(client, create_session, ws_conv):
    sid = create_session([script(text="the answer")])
    with ws_conv(sid) as conv:
        conv.invoke("what is ginno?")
        conv.recv_until("message.end", "error")

    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "what is ginno?" in msgs[0]["blocks"][0]["text"]
    assert any(b["kind"] == "text" and "the answer" in b["text"] for b in msgs[1]["blocks"])
    # assistant bubble carries the session's agent so it renders with a persona
    assert msgs[1]["agentId"] == "dev"


def test_history_folds_tool_call_into_one_bubble(client, create_session, ws_conv, isolated_home):
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("read_file", {"path": "a.txt"})]),
            script(text="done reading"),
        ]
    )
    # fixture lives in the session workspace (the tools' bound cwd, plan F1)
    from ginno_runtime import paths

    (paths.session_files_dir("default", sid) / "a.txt").write_text(
        "HELLO_HISTORY", encoding="utf-8"
    )
    with ws_conv(sid) as conv:
        conv.invoke("read the file")
        conv.recv_until("message.end", "error")

    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert msgs[0]["role"] == "user"
    asst = msgs[1]
    kinds = [b["kind"] for b in asst["blocks"]]
    # tool result folded into the same assistant bubble as the final text (one bubble/turn)
    assert "tool" in kinds and "text" in kinds
    tool_block = next(b for b in asst["blocks"] if b["kind"] == "tool")
    assert tool_block["name"] == "read_file"
    assert "HELLO_HISTORY" in tool_block["content"]
    assert tool_block["pending"] is False
