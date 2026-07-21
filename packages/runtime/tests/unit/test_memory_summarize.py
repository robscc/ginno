"""Unit tests for memory summarization (with fake model)."""

from __future__ import annotations

import pytest

from ginno_runtime.memory.pool import append_to_pool, clear_pool, pool_count, read_pool
from ginno_runtime.memory.summarize import _read_existing_memory, _write_memory, summarize_pool
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.unit


def test_read_existing_memory_skips_boilerplate(isolated_home):
    # default boilerplate should be skipped
    assert _read_existing_memory() == ""


def test_write_and_read_memory(isolated_home):
    _write_memory("## 主题\n- 知识点1\n- 知识点2")
    assert "知识点1" in _read_existing_memory()


@pytest.mark.asyncio
async def test_summarize_pool_empty(isolated_home, monkeypatch):
    # empty pool → ok with message
    result = await summarize_pool()
    assert result["ok"] is True
    assert result["pool_entries"] == 0
    assert "pool empty" in result.get("message", "")


@pytest.mark.asyncio
async def test_summarize_pool_with_fake_model(isolated_home, monkeypatch):
    # append some pool entries
    append_to_pool("s1", "dev", "用户偏好使用 TypeScript 而非 JavaScript")
    append_to_pool("s1", "dev", "项目使用 pnpm 作为包管理器")
    assert pool_count() == 2

    # patch build_model to return a fake model that produces a summary
    fake_summary = "## 技术栈\n- TypeScript\n- pnpm"
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    fake_model = ScriptedChatModel(scripts=[script(text=fake_summary)])
    monkeypatch.setattr("ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake_model)

    result = await summarize_pool()
    assert result["ok"] is True
    assert result["pool_entries"] == 2
    assert result["summarized_chars"] > 0

    # pool should be cleared after summarization
    assert pool_count() == 0

    # MEMORY.md should contain the summary
    mem = _read_existing_memory()
    assert "TypeScript" in mem
    assert "pnpm" in mem
