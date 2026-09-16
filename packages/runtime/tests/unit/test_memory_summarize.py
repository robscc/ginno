"""Unit tests for gated distillation: create_draft (never touches MEMORY.md)."""

from __future__ import annotations

import pytest

from ginno_runtime.memory.pool import append_to_pool, pool_count, read_pool
from ginno_runtime.memory.summarize import (
    _read_existing_memory,
    _write_memory,
    create_draft,
    has_draft,
)
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.unit


class RecordingModel:
    """Wraps a scripted model and records every ainvoke message list."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[list] = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        return await self.inner.ainvoke(messages, **kwargs)


def _patch_model(monkeypatch, text: str) -> RecordingModel:
    rec = RecordingModel(ScriptedChatModel(scripts=[script(text=text)]))
    monkeypatch.setattr(
        "ginno_runtime.memory.summarize.build_model", lambda *a, **k: rec
    )
    return rec


def test_read_existing_memory_skips_boilerplate(isolated_home):
    # default boilerplate should be skipped
    assert _read_existing_memory() == ""


def test_write_and_read_memory(isolated_home):
    _write_memory("## 主题\n- 知识点1\n- 知识点2")
    assert "知识点1" in _read_existing_memory()


@pytest.mark.asyncio
async def test_create_draft_empty_pool(isolated_home):
    result = await create_draft()
    assert result["ok"] is True
    assert result["pool_entries"] == 0
    assert "pool empty" in result.get("message", "")
    assert not has_draft()


@pytest.mark.asyncio
async def test_create_draft_does_not_touch_memory_or_pool(isolated_home, monkeypatch):
    append_to_pool("s1", "dev", "用户偏好使用 TypeScript 而非 JavaScript")
    append_to_pool("s1", "dev", "项目使用 pnpm 作为包管理器")
    _patch_model(monkeypatch, "## 技术栈\n- TypeScript\n- pnpm")

    result = await create_draft()
    assert result["ok"] is True
    assert result["pool_entries"] == 2
    # The draft carries the distilled text + review metadata…
    assert "TypeScript" in result["draft"]
    assert result["chars"] > 0
    assert result["budget"] == 3000
    assert result["over_budget"] is False
    assert isinstance(result["diff"], str)
    # …but MEMORY.md and the pool are untouched until apply (quality gate).
    assert _read_existing_memory() == ""
    assert pool_count() == 2
    assert has_draft()


@pytest.mark.asyncio
async def test_create_draft_reuses_pending_draft(isolated_home, monkeypatch):
    append_to_pool("s1", "dev", "内容")
    rec = _patch_model(monkeypatch, "## A\n- x")

    first = await create_draft()
    assert first["ok"] is True
    # Second trigger while a draft is pending: reuse, no second LLM call.
    second = await create_draft()
    assert second["ok"] is True
    assert second.get("draft_exists") is True
    assert second["draft"] == first["draft"]
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_create_draft_over_budget_flag(isolated_home, monkeypatch):
    append_to_pool("s1", "dev", "内容")
    _patch_model(monkeypatch, "字" * 3500)  # exceeds the 3000-char budget
    result = await create_draft()
    assert result["ok"] is True
    assert result["over_budget"] is True


@pytest.mark.asyncio
async def test_create_draft_marks_cited_excerpts(isolated_home, monkeypatch):
    # Gate 1 signal: verified-citation turns get an evidence prefix in the
    # distillation prompt.
    append_to_pool("s1", "dev", "带引用的结论", cited=True)
    append_to_pool("s1", "dev", "普通结论")
    rec = _patch_model(monkeypatch, "## A\n- x")

    await create_draft()
    human = [m for m in rec.calls[0] if m.type == "human"][0]
    assert "[有引用验证] 带引用的结论" in human.content
    assert "[有引用验证] 普通结论" not in human.content


@pytest.mark.asyncio
async def test_create_draft_kb_digest_when_usable(isolated_home, kb_vault, monkeypatch):
    # B1: when the KB is configured and the usage ledger has rows, the prompt
    # carries the ledger digest so covered facts aren't duplicated.
    from ginno_runtime.knowledge import usage as kb_usage

    kb_usage.record_injected(["Ginno/Wiki/concepts/permission.md"])
    kb_usage.record_cited("Ginno/Wiki/concepts/permission.md", "s", "t1")
    append_to_pool("s1", "dev", "权限节点相关内容")
    rec = _patch_model(monkeypatch, "## A\n- x")

    await create_draft()
    human = [m for m in rec.calls[0] if m.type == "human"][0]
    assert "近期知识库使用台账" in human.content
    assert "Ginno/Wiki/concepts/permission.md" in human.content


@pytest.mark.asyncio
async def test_create_draft_no_digest_when_kb_disabled(isolated_home, monkeypatch):
    append_to_pool("s1", "dev", "内容")
    rec = _patch_model(monkeypatch, "## A\n- x")
    await create_draft()
    human = [m for m in rec.calls[0] if m.type == "human"][0]
    assert "近期知识库使用台账" not in human.content
