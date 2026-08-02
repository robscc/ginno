"""Tests for `strip_old_images` — bounding the LLM context for multi-image sessions.

Only the most recent K user turns keep their image blocks; older turns' images
are replaced by a text placeholder. The trim applies to the COPY sent to the
model — the persisted checkpoint must keep every image (UI history intact).
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime import paths
from ginno_runtime.graph import strip_old_images
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.unit


def _img(data: str) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _human(text: str, *imgs: str) -> HumanMessage:
    content: list = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(_img(d) for d in imgs)
    return HumanMessage(content=content)


def _has_image(m) -> bool:
    return isinstance(m.content, list) and any(
        isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in m.content
    )


# ── pure-function tests ──────────────────────────────────────────────────────


def test_keeps_recent_turns_strips_older():
    msgs = [_human("one", "A"), _human("two", "B"), _human("three", "C")]
    out = strip_old_images(msgs, keep_turns=2)
    assert len(out) == 3
    # newest two keep their images
    assert _has_image(out[1]) and _has_image(out[2])
    # oldest is stripped: text kept, image gone, placeholder added
    assert not _has_image(out[0])
    texts = [b["text"] for b in out[0].content if b.get("type") == "text"]
    assert "one" in texts
    assert any("张历史图片已省略" in t for t in texts)


def test_image_only_message_leaves_nonempty_placeholder():
    msgs = [_human("", "A"), _human("recent", "B")]
    out = strip_old_images(msgs, keep_turns=1)
    assert out[0].content, "stripped message must not be empty"
    assert out[0].content == [{"type": "text", "text": "[1 张历史图片已省略]"}]


def test_does_not_mutate_input():
    orig = _human("one", "A")
    msgs = [orig, _human("two", "B")]
    out = strip_old_images(msgs, keep_turns=1)
    # original object untouched
    assert _has_image(orig)
    assert out[0] is not orig
    # returned list is a new object
    assert out is not msgs


def test_keep_turns_zero_strips_all():
    msgs = [_human("one", "A"), _human("two", "B")]
    out = strip_old_images(msgs, keep_turns=0)
    assert not any(_has_image(m) for m in out)


def test_string_content_passthrough_and_ai_untouched():
    msgs = [HumanMessage(content="plain text"), AIMessage(content="reply")]
    out = strip_old_images(msgs, keep_turns=0)
    assert out[0].content == "plain text"
    assert out[1] is msgs[1]  # non-human messages pass through unchanged


# ── end-to-end: real graph + checkpointer ────────────────────────────────────


class _RecordingModel(ScriptedChatModel):
    """ScriptedChatModel that records the message list of every LLM call."""

    recorded: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.recorded.append([getattr(m, "content", None) for m in messages])
        return super()._generate(messages, stop, run_manager, **kwargs)


async def test_graph_trims_context_but_checkpoint_keeps_images(isolated_home):
    paths.ensure_layout()
    from ginno_runtime.graph import build_graph

    model = _RecordingModel(scripts=[script(text="r1"), script(text="r2"), script(text="r3")])
    graph = build_graph(model=model, project_slug="p", workspace="/tmp", mcp_tools=[])
    cfg = {"configurable": {"thread_id": "img", "project_slug": "p", "agent_id": "dev"}}

    for txt, img in [("t1", "IMGAAA"), ("t2", "IMGBBB"), ("t3", "IMGCCC")]:
        await graph.ainvoke(
            {
                "messages": [_human(txt, img)],
                "workspace": "/tmp",
                "project_slug": "p",
                "agent_id": "dev",
                "active_skills": [],
                "pending_tool_calls": [],
            },
            config=cfg,
        )

    # The final turn's LLM call: turn-1 image stripped, turns 2 & 3 kept (K=2).
    blob = json.dumps(model.recorded[-1], ensure_ascii=False, default=str)
    assert "IMGAAA" not in blob, "oldest turn's image must not reach the model"
    assert "IMGBBB" in blob and "IMGCCC" in blob, "recent turns keep their images"
    assert "张历史图片已省略" in blob

    # The persisted checkpoint keeps EVERY image — state was never mutated.
    # Load it back through the checkpointer (the same path the UI history uses)
    # and confirm the deserialized messages still carry all three images.
    from ginno_runtime.checkpointer import FileCheckpointer

    tup = FileCheckpointer(project_slug="p").get_tuple(
        {"configurable": {"thread_id": "img"}}
    )
    stored = tup.checkpoint["channel_values"]["messages"]
    stored_blob = json.dumps(
        [getattr(m, "content", None) for m in stored], ensure_ascii=False, default=str
    )
    assert "IMGAAA" in stored_blob and "IMGBBB" in stored_blob and "IMGCCC" in stored_blob
