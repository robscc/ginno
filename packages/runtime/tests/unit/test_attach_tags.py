"""``<attach_ref .../>`` written as text instead of called as a tool.

Observed live on deepseek-flash (2026-10-23, session 4e5b9f89): the model
answered with XML-ish tags in the final prose, so the bubble showed raw
markup and no chip appeared. The tag is parsed back into a ref (live stream +
history replay) and stripped from the visible text.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime.api.messages_ui import _ai_content_blocks, _messages_to_ui
from ginno_runtime.tools.render_tools import AttachTagStreamFilter, split_attach_tags

pytestmark = pytest.mark.unit

_TAG = (
    '<attach_ref kind="doc" name="README.md（方案+用法）" '
    'ref_id="/ws/app-iter-harness/README.md"/>'
)


def test_split_single_tag_keeps_prose():
    clean, refs = split_attach_tags(f"已交付：\n{_TAG}")
    assert clean == "已交付：\n"
    assert refs == [
        {
            "kind": "doc",
            "name": "README.md（方案+用法）",
            "ref_id": "/ws/app-iter-harness/README.md",
        }
    ]


def test_split_multiple_tags_and_trailing_prose():
    text = f"{_TAG}\n<attach_ref kind='file' name='a.py' ref_id='/a.py'/> 完"
    clean, refs = split_attach_tags(text)
    assert clean == "\n 完"
    assert [r["name"] for r in refs] == ["README.md（方案+用法）", "a.py"]


def test_malformed_tag_stays_text():
    # No recognizable attribute → prose that merely mentions the name.
    clean, refs = split_attach_tags("<attach_ref 这不是调用>")
    assert refs == []
    assert clean == "<attach_ref 这不是调用>"


def test_stream_filter_reassembles_split_tag():
    f = AttachTagStreamFilter()
    parts = [
        "先说结论。\n<attach",
        '_ref kind="file" name="harness.py" ',
        'ref_id="/ws/harness.py"/>',
        "\n完后再说两句。",
    ]
    text, refs = "", []
    for p in parts:
        t, r = f.feed(p)
        text += t
        refs += r
    t, r = f.flush()
    text += t
    refs += r
    assert "<attach_ref" not in text
    assert text == "先说结论。\n\n完后再说两句。"
    assert refs == [
        {"kind": "file", "name": "harness.py", "ref_id": "/ws/harness.py"}
    ]


def test_stream_filter_holds_partial_prefix():
    f = AttachTagStreamFilter()
    assert f.feed("hi <att")[0] == "hi "
    assert f.feed("ach_ref")[0] == ""
    t, refs = f.feed(' kind="file" name="x" ref_id="/x"/>')
    assert (t, refs) == ("", [{"kind": "file", "name": "x", "ref_id": "/x"}])


def test_stream_filter_flush_releases_unterminated_tag_as_text():
    f = AttachTagStreamFilter()
    assert f.feed("打断 <attach_ref kind=")[0] == "打断 "
    assert f.flush() == ("<attach_ref kind=", [])


def test_history_replay_renders_ref_blocks_and_strips_tags():
    msg = AIMessage(content=f"交付如下。\n{_TAG}")
    blocks = _ai_content_blocks(msg.content)
    assert [b["kind"] for b in blocks] == ["text", "ref"]
    assert blocks[0]["text"] == "交付如下。"
    assert blocks[1] == {
        "kind": "ref",
        "refKind": "doc",
        "name": "README.md（方案+用法）",
        "refId": "/ws/app-iter-harness/README.md",
    }


def test_history_replay_dedupes_ref_from_tool_call_and_tag():
    msg = AIMessage(
        content=f"交付。\n{_TAG}",
        tool_calls=[
            {
                "name": "attach_ref",
                "args": {
                    "kind": "doc",
                    "name": "README.md（方案+用法）",
                    "ref_id": "/ws/app-iter-harness/README.md",
                },
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    ui = _messages_to_ui([HumanMessage("go"), msg], "dev")
    refs = [b for b in ui[1]["blocks"] if b.get("kind") == "ref"]
    assert len(refs) == 1
    assert not any(
        b.get("kind") == "text" and "attach_ref" in (b.get("text") or "")
        for b in ui[1]["blocks"]
    )


def test_element_style_closing_tag_is_dropped():
    text = '<attach_ref kind="file" name="a.py" ref_id="/a.py"></attach_ref>'
    clean, refs = split_attach_tags(text)
    assert clean == ""
    assert refs == [{"kind": "file", "name": "a.py", "ref_id": "/a.py"}]


def test_stream_filter_holds_split_closing_tag():
    f = AttachTagStreamFilter()
    out, refs = f.feed('<attach_ref kind="file" name="a.py" ref_id="/a.py">')
    assert refs and refs[0]["name"] == "a.py"
    t, r = f.feed("</attach")
    assert (t, r) == ("", [])
    t, r = f.feed("_ref> done")
    assert t == " done"
