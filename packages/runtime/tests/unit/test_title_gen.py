"""Unit tests for the LLM session title generator (title_gen)."""

from __future__ import annotations

import pytest

from ginno_runtime import title_gen
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.unit

_PENDING = {
    "id": "s1",
    "title_llm_pending": True,
    "title_seed": "what is photosynthesis",
    "title_assistant": "Photosynthesis converts light into chemical energy.",
    "provider": "custom",
    "model": "fake",
}


def _patch_model(monkeypatch, model) -> list:
    """Route title_gen's model construction to a fake; returns the call args."""
    calls: list = []

    def fake_build(provider, model_name=None, **kw):
        calls.append((provider, model_name))
        return model

    monkeypatch.setattr(title_gen, "build_model", fake_build)
    return calls


def test_sanitize_title():
    assert title_gen._sanitize_title("") == ""
    assert title_gen._sanitize_title("   \n ") == ""
    # first line only, whitespace collapsed
    assert title_gen._sanitize_title("  GPU 集群 部署\n第二行不要") == "GPU 集群 部署"
    # markdown leftovers + wrapping quotes / emphasis
    assert title_gen._sanitize_title('# "Topic"') == "Topic"
    assert title_gen._sanitize_title("- '单引号主题'") == "单引号主题"
    assert title_gen._sanitize_title("“Curly quoted”") == "Curly quoted"
    assert title_gen._sanitize_title("‘single curly’") == "single curly"
    assert title_gen._sanitize_title("`code topic`") == "code topic"
    assert title_gen._sanitize_title('"**Photosynthesis**"') == "Photosynthesis"
    assert title_gen._sanitize_title("__underline__") == "underline"
    # stringified python structures are not titles
    assert title_gen._sanitize_title("['', {'thinking': 'x'}]") == ""
    assert title_gen._sanitize_title('{"title": "x"}') == ""
    # 60-char cap
    assert len(title_gen._sanitize_title("x" * 100)) == 60


def test_content_text_drops_thinking_blocks():
    class _Resp:
        content = [
            "",
            {"thinking": "let me think..."},
            {"type": "text", "text": "GPU 集群部署验收清单"},
        ]

    assert title_gen._content_text(_Resp()) == "GPU 集群部署验收清单"

    class _Str:
        content = "plain"

    assert title_gen._content_text(_Str()) == "plain"


async def test_maybe_llm_title_success(monkeypatch):
    meta = dict(_PENDING)
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: (dict(meta), "default"))
    patches: list = []

    def fake_patch(slug, sid, patch):
        patches.append((slug, sid, patch))
        meta.update(patch)
        return dict(meta)

    monkeypatch.setattr(title_gen, "_session_meta_patch", fake_patch)
    events: list = []

    async def fake_push(sid, kind, payload, turn_id):
        events.append((kind, payload))

    monkeypatch.setattr(title_gen, "_push_session_event", fake_push)
    model = ScriptedChatModel(scripts=[script(text='"**Photosynthesis**"')])
    calls = _patch_model(monkeypatch, model)
    await title_gen.maybe_llm_title("s1", "t1")
    # auxiliary call uses the session's persisted provider/model
    assert calls == [("custom", "fake")]
    slug, sid, patch = patches[-1]
    assert slug == "default" and sid == "s1"
    assert patch["title"] == "Photosynthesis"
    assert patch["title_llm_pending"] is False
    assert patch["title_seed"] == "" and patch["title_assistant"] == ""
    assert events == [("session_title", {"title": "Photosynthesis"})]


async def test_maybe_llm_title_aborts_without_pending(monkeypatch):
    meta = dict(_PENDING, title_llm_pending=False)
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: (meta, "default"))
    patches: list = []
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: patches.append(a))
    await title_gen.maybe_llm_title("s1", "t1")
    assert patches == []


async def test_maybe_llm_title_aborts_without_seed(monkeypatch):
    meta = dict(_PENDING, title_seed="  ")
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: (meta, "default"))
    patches: list = []
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: patches.append(a))
    await title_gen.maybe_llm_title("s1", "t1")
    assert patches == []


async def test_maybe_llm_title_aborts_when_deleted(monkeypatch):
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: None)
    patches: list = []
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: patches.append(a))
    await title_gen.maybe_llm_title("s1", "t1")
    assert patches == []


async def test_maybe_llm_title_skips_when_no_model(monkeypatch):
    # disabled/unknown provider -> build_model raises -> placeholder stays
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: (dict(_PENDING), "default"))

    def boom(*a, **kw):
        raise ValueError("provider custom is disabled (enable it in Settings)")

    monkeypatch.setattr(title_gen, "build_model", boom)
    patches: list = []
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: patches.append(a))
    await title_gen.maybe_llm_title("s1", "t1")  # must not raise
    assert patches == []


async def test_maybe_llm_title_rename_race_wins(monkeypatch):
    # pending at first read, cleared by a manual rename while ainvoke ran:
    # the write-time re-read must abort without patching.
    reads = [
        (dict(_PENDING), "default"),
        (dict(_PENDING, title_llm_pending=False, title="Pinned"), "default"),
    ]
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: reads.pop(0))
    patches: list = []
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: patches.append(a))
    _patch_model(monkeypatch, ScriptedChatModel(scripts=[script(text="Topic")]))
    await title_gen.maybe_llm_title("s1", "t1")
    assert patches == []


class _BoomModel:
    async def ainvoke(self, *a, **k):
        raise RuntimeError("provider down")


async def test_run_title_gen_swallows_model_failure(monkeypatch):
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: (dict(_PENDING), "default"))
    patches: list = []
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: patches.append(a))
    _patch_model(monkeypatch, _BoomModel())
    await title_gen._run_title_gen("s1", "t1")  # must not raise
    assert patches == []
    assert "s1" not in title_gen._TITLE_INFLIGHT


async def test_maybe_llm_title_deleted_at_write_skips_event(monkeypatch):
    monkeypatch.setattr(title_gen, "_find_meta", lambda s: (dict(_PENDING), "default"))
    monkeypatch.setattr(title_gen, "_session_meta_patch", lambda *a: None)
    events: list = []

    async def fake_push(sid, kind, payload, turn_id):
        events.append(kind)

    monkeypatch.setattr(title_gen, "_push_session_event", fake_push)
    _patch_model(monkeypatch, ScriptedChatModel(scripts=[script(text="Topic")]))
    await title_gen.maybe_llm_title("s1", "t1")
    assert events == []


def test_spawn_title_gen_dedupes(monkeypatch):
    calls: list = []

    def fake_spawn(coro):
        coro.close()
        calls.append(1)

    monkeypatch.setattr(title_gen, "spawn_bg", fake_spawn)
    title_gen._TITLE_INFLIGHT.add("s1")
    try:
        title_gen.spawn_title_gen("s1", "t1")
        assert calls == []  # in-flight guard blocks the duplicate
        title_gen._TITLE_INFLIGHT.discard("s1")
        title_gen.spawn_title_gen("s1", "t1")
        assert calls == [1]
    finally:
        title_gen._TITLE_INFLIGHT.discard("s1")
