"""Issue #6: prompt hook 改写必须进入最终 HumanMessage（content 与 user_text
同步）——纯文本 / 图片多模态 / attachment-only 三条 content 构建路径都要覆盖。

断言的是**真正传给 graph.astream 的入图消息**，不是 hook 的返回值。
"""

from __future__ import annotations

import pytest

from langchain_core.messages import HumanMessage

import ginno_runtime.api.stream.turn as stream_mod
from ginno_runtime.api.stream.turn import _attach_only_text
import ginno_runtime.mods.events as mods_events

pytestmark = pytest.mark.unit

SLUG = "default"
_REWRITTEN = "hook改写后的正文"


class _CaptureGraph:
    """astream stand-in: record the input messages, yield nothing."""

    def __init__(self):
        self.inputs: list = []

    def astream(self, input_state, config=None, stream_mode=None):
        graph = self

        async def gen():
            graph.inputs.append(input_state)
            return
            yield None  # pragma: no cover

        return gen()

    async def aget_state(self, config):
        class _Snap:
            next = ()
            tasks = ()
            values = {}
        return _Snap()


def _session(sid: str, graph) -> dict:
    return {
        "session_id": sid,
        "project_slug": SLUG,
        "workspace": "/tmp",
        "agent_id": "dev",
        "graph": graph,
        "context_dirs": [],
        "primary_path": "",
    }


@pytest.fixture
def hook_env(monkeypatch):
    """Fake prompt.submit rewrite + fake the WS push edge."""
    sent: list = []

    async def fake_push(session_id, event, data, turn_id=None):
        sent.append((event, data))

    monkeypatch.setattr(stream_mod, "_push_session_event", fake_push)

    async def fake_dispatch(session_id, text):
        return mods_events.PromptSubmitResult(text=_REWRITTEN)

    monkeypatch.setattr(mods_events, "dispatch_prompt_submit", fake_dispatch)
    return sent


async def _run(sid: str, graph, text: str, **kw) -> None:
    await stream_mod._run_stream(
        None, graph, {"configurable": {"thread_id": sid, "project_slug": SLUG, "turn_id": "t1"}},
        text, _session(sid, graph), "dev", **kw,
    )


async def test_plain_text_rewrite_enters_graph(hook_env):
    graph = _CaptureGraph()
    await _run("h1", graph, "原始正文")
    assert graph.inputs
    humans = [m for m in graph.inputs[0]["messages"] if isinstance(m, HumanMessage)]
    assert humans and humans[-1].content == _REWRITTEN


async def test_multimodal_text_part_rewritten(hook_env):
    graph = _CaptureGraph()
    imgs = [{"data": "aWNvbg==", "media_type": "image/png"}]
    await _run("h2", graph, "看这张图", images=imgs)
    content = graph.inputs[0]["messages"][-1].content
    assert isinstance(content, list)
    texts = [p["text"] for p in content if p.get("type") == "text"]
    assert texts == [_REWRITTEN]
    # image parts preserved
    assert any(p.get("type") == "image_url" for p in content)


def test_attach_only_rewrite_paths():
    """attachment-only（无正文只带附件）：hook 空改写不覆盖合成意图文案；
    hook 真给了文本则采纳。"""
    from ginno_runtime.api.stream.turn import _sync_rewrite_into_content

    synth = _attach_only_text()
    assert _sync_rewrite_into_content(synth, "", imgs=[], attach_only=True) == synth
    assert (
        _sync_rewrite_into_content(synth, "分析这些附件", imgs=[], attach_only=True)
        == "分析这些附件"
    )


async def test_no_rewrite_keeps_content_unchanged(hook_env, monkeypatch):
    """hook 未改写（text 原样返回）时 content 构建路径不受影响。"""
    async def passthrough(session_id, text):
        return mods_events.PromptSubmitResult(text=text)

    monkeypatch.setattr(mods_events, "dispatch_prompt_submit", passthrough)
    graph = _CaptureGraph()
    imgs = [{"data": "aWNvbg==", "media_type": "image/png"}]
    await _run("h4", graph, "原图文", images=imgs)
    content = graph.inputs[0]["messages"][-1].content
    texts = [p["text"] for p in content if p.get("type") == "text"]
    assert texts == ["原图文"]
