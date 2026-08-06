"""Unit tests for multimodal message serialization helpers.

Covers the content -> UI block conversion used by the session history
endpoint (server.py) and the text extraction used for wiki retrieval
(graph.py), including OpenAI-style image_url and Anthropic-native image
blocks.
"""

from __future__ import annotations

import base64

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime.graph import _latest_human_text, text_of_content
from ginno_runtime.server import (
    TOOL_OUTPUT_WS_LIMIT,
    _ai_content_blocks,
    _content_ui_blocks,
    _image_block_url,
    _messages_to_ui,
    _tool_content_str,
    _truncate_for_ws,
)

B64 = base64.b64encode(b"fake-image-bytes").decode()


# --------------------------------------------------------------------------- #
# text_of_content / _latest_human_text
# --------------------------------------------------------------------------- #
def test_text_of_content_str_passthrough():
    assert text_of_content("plain") == "plain"


def test_text_of_content_joins_text_blocks_and_skips_images():
    content = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
        {"type": "text", "text": "second"},
        "bare-string",
    ]
    assert text_of_content(content) == "first\nsecond\nbare-string"


def test_text_of_content_empty_list():
    assert text_of_content([]) == ""


def test_latest_human_text_multimodal_message():
    msgs = [
        HumanMessage(
            content=[
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
                {"type": "text", "text": "what does this graph show?"},
            ]
        )
    ]
    assert _latest_human_text(msgs) == "what does this graph show?"


# --------------------------------------------------------------------------- #
# _image_block_url
# --------------------------------------------------------------------------- #
def test_image_block_url_openai_style():
    b = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
    assert _image_block_url(b) == "data:image/png;base64,xx"


def test_image_block_url_anthropic_base64_source():
    b = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": B64},
    }
    assert _image_block_url(b) == f"data:image/jpeg;base64,{B64}"


def test_image_block_url_anthropic_url_source():
    b = {"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}}
    assert _image_block_url(b) == "https://x.test/a.png"


def test_image_block_url_garbage_returns_none():
    assert _image_block_url({"type": "image_url"}) is None
    assert _image_block_url({"type": "image", "source": {}}) is None


# --------------------------------------------------------------------------- #
# _content_ui_blocks / _ai_content_blocks
# --------------------------------------------------------------------------- #
def test_content_ui_blocks_str():
    assert _content_ui_blocks("hello") == [{"kind": "text", "text": "hello"}]
    assert _content_ui_blocks("   ") == []


def test_content_ui_blocks_multimodal():
    blocks = _content_ui_blocks(
        [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
        ]
    )
    assert blocks == [
        {"kind": "text", "text": "look"},
        {"kind": "image", "url": f"data:image/png;base64,{B64}"},
    ]


def test_ai_content_blocks_includes_thinking_text_and_image():
    blocks = _ai_content_blocks(
        [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": B64}},
        ]
    )
    assert blocks[0] == {"kind": "thinking", "text": "hmm"}
    assert blocks[1] == {"kind": "text", "text": "answer"}
    assert blocks[2]["kind"] == "image"
    assert blocks[2]["url"] == f"data:image/png;base64,{B64}"


# --------------------------------------------------------------------------- #
# _tool_content_str / _truncate_for_ws
# --------------------------------------------------------------------------- #
def test_tool_content_str_str_passthrough():
    assert _tool_content_str("ok") == "ok"
    assert _tool_content_str(None) == ""


def test_tool_content_str_list_with_image_marker():
    content = [
        {"type": "text", "text": "captured screenshot"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
        "tail",
    ]
    assert _tool_content_str(content) == "captured screenshot\n[image]\ntail"


def test_truncate_for_ws_short_passthrough():
    assert _truncate_for_ws("short") == "short"


def test_truncate_for_ws_long_appends_marker():
    text = "x" * (TOOL_OUTPUT_WS_LIMIT + 2000)
    out = _truncate_for_ws(text)
    assert out.startswith("x" * TOOL_OUTPUT_WS_LIMIT)
    assert "已截断" in out
    assert str(len(text)) in out
    assert len(out) < len(text)


# --------------------------------------------------------------------------- #
# _messages_to_ui (history serializer) round trip
# --------------------------------------------------------------------------- #
def test_messages_to_ui_multimodal_user_and_ai():
    human = HumanMessage(
        content=[
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
        ],
        id="h1",
    )
    ai = AIMessage(
        content=[
            {"type": "text", "text": "I see a chart"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": B64}},
        ],
        id="a1",
    )
    ui = _messages_to_ui([human, ai], "dev")
    assert ui[0]["role"] == "user"
    assert ui[0]["blocks"][0] == {"kind": "text", "text": "look at this"}
    assert ui[0]["blocks"][1] == {"kind": "image", "url": f"data:image/png;base64,{B64}"}
    assert ui[1]["role"] == "assistant"
    assert {"kind": "text", "text": "I see a chart"} in ui[1]["blocks"]
    imgs = [b for b in ui[1]["blocks"] if b["kind"] == "image"]
    assert imgs and imgs[0]["url"] == f"data:image/jpeg;base64,{B64}"


def test_messages_to_ui_tool_message_list_content_is_stringified():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "screenshot", "args": {}, "id": "t1", "type": "tool_call"}],
        id="a1",
    )
    tm = ToolMessage(
        content=[
            {"type": "text", "text": "captured"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
        ],
        tool_call_id="t1",
    )
    ui = _messages_to_ui([ai, tm], None)
    tool = next(b for b in ui[0]["blocks"] if b["kind"] == "tool")
    assert tool["content"] == "captured\n[image]"
    assert tool["name"] == "screenshot"


def test_messages_to_ui_replays_chart_widget():
    chart_data = {
        "type": "line",
        "title": "visits",
        "x": "day",
        "y": "n",
        "data": [{"day": "Mon", "n": 3}, {"day": "Tue", "n": 7}],
    }
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "render_widget", "args": {"kind": "chart", "data": chart_data}, "id": "t1", "type": "tool_call"}
        ],
        id="a1",
    )
    tm = ToolMessage(content="[rendered widget: chart]", tool_call_id="t1")
    ui = _messages_to_ui([ai, tm], None)
    widgets = [b for b in ui[0]["blocks"] if b["kind"] == "widget"]
    assert len(widgets) == 1
    assert widgets[0]["widgetKind"] == "chart"
    assert widgets[0]["data"] == chart_data
    # render_widget is silent on replay too: no ordinary tool bubble
    assert not [b for b in ui[0]["blocks"] if b["kind"] == "tool"]
