"""WebSocket end-to-end: multimodal messages and long tool output truncation.

Drives the real graph with the scripted fake LLM: invoke payloads carrying
base64 images must round-trip through the checkpointer into the history
endpoint's UI blocks, and oversized live tool output must be capped with a
visible truncation marker.
"""

from __future__ import annotations

import base64

import pytest

from conftest import events_of
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e

B64 = base64.b64encode(b"fake-png-bytes").decode()


def test_invoke_with_images_round_trips_through_history(client, create_session, ws_conv):
    sid = create_session([script(text="I see a diagram.")])
    with ws_conv(sid) as conv:
        conv.send(
            {
                "type": "invoke",
                "message": "what is this?",
                "images": [{"data": B64, "media_type": "image/png"}],
            }
        )
        events = conv.recv_until("message.end", "error")
    assert "error" not in [e["event"] for e in events]

    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    user = msgs[0]
    assert user["role"] == "user"
    assert user["blocks"][0] == {"kind": "text", "text": "what is this?"}
    img = next(b for b in user["blocks"] if b["kind"] == "image")
    assert img["url"] == f"data:image/png;base64,{B64}"


def test_invoke_images_only_message_has_image_block(client, create_session, ws_conv):
    # Empty text + image: the user entry exists with only an image block.
    sid = create_session([script(text="ok")])
    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "", "images": [{"data": B64}]})
        conv.recv_until("message.end", "error")

    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert msgs[0]["role"] == "user"
    assert any(b["kind"] == "image" for b in msgs[0]["blocks"])
    assert not any(b["kind"] == "text" for b in msgs[0]["blocks"])


def test_tool_end_truncates_long_output_with_marker(create_session, ws_conv):
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("read_file", {"path": "big.txt"})]),
            script(text="done"),
        ]
    )
    from ginno_runtime import paths

    (paths.session_files_dir("default", sid) / "big.txt").write_text("y" * 6000)
    with ws_conv(sid) as conv:
        conv.invoke("read it")
        events = conv.recv_until("message.end", "error")
    end = events_of(events, "tool.end")[0]
    assert len(end["content"]) < 6000
    assert "已截断" in end["content"]


def test_tool_output_does_not_leak_into_text_deltas(create_session, ws_conv):
    # Regression: ToolMessage results must reach the UI only via tool.end, never
    # as token.delta (which would render the tool output as assistant text).
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("read_file", {"path": "leak.txt"})]),
            script(text="FINAL_ANSWER_TEXT"),
        ]
    )
    from ginno_runtime import paths

    (paths.session_files_dir("default", sid) / "leak.txt").write_text("LEAK_MARKER_TOKEN\n")
    with ws_conv(sid) as conv:
        conv.invoke("read it")
        events = conv.recv_until("message.end", "error")
    deltas = "".join(e.get("content", "") for e in events_of(events, "token.delta"))
    assert "FINAL_ANSWER_TEXT" in deltas
    assert "LEAK_MARKER_TOKEN" not in deltas
    end = events_of(events, "tool.end")[0]
    assert "LEAK_MARKER_TOKEN" in end["content"]
