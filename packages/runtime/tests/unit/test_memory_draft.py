"""Unit tests for the draft review lifecycle: apply / discard / staleness."""

from __future__ import annotations

import time

import pytest

from ginno_runtime import paths
from ginno_runtime.memory.pool import append_to_pool, pool_count, read_pool
from ginno_runtime.memory.summarize import (
    _read_existing_memory,
    apply_draft,
    create_draft,
    discard_draft,
    has_draft,
    read_draft,
    remove_memory_lines,
)
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.unit


def _patch_model(monkeypatch, text: str) -> None:
    fake = ScriptedChatModel(scripts=[script(text=text)])
    monkeypatch.setattr(
        "ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake
    )


@pytest.mark.asyncio
async def _make_draft(monkeypatch, text="## 技术栈\n- TypeScript") -> dict:
    append_to_pool("s1", "dev", "偏好 TypeScript")
    _patch_model(monkeypatch, text)
    return await create_draft()


@pytest.mark.asyncio
async def test_apply_draft_writes_memory_clears_pool_deletes_draft(isolated_home, monkeypatch):
    await _make_draft(monkeypatch)
    assert has_draft()

    result = apply_draft()
    assert result["ok"] is True
    assert result["pool_remaining"] == 0
    assert "TypeScript" in _read_existing_memory()
    assert pool_count() == 0
    assert not has_draft()


@pytest.mark.asyncio
async def test_apply_draft_with_edited_content(isolated_home, monkeypatch):
    await _make_draft(monkeypatch)
    result = apply_draft(content="## 技术栈\n- TypeScript（编辑过）")
    assert result["ok"] is True
    assert "编辑过" in _read_existing_memory()


@pytest.mark.asyncio
async def test_apply_draft_without_draft(isolated_home):
    result = apply_draft()
    assert result["ok"] is False
    assert result["error"] == "no_draft"


@pytest.mark.asyncio
async def test_apply_draft_guards_stale_memory(isolated_home, monkeypatch):
    await _make_draft(monkeypatch)
    # MEMORY.md changed after the draft was created (e.g. manual edit).
    paths.memory_index_path().write_text("## 别的记忆\n- 新内容", encoding="utf-8")

    result = apply_draft()
    assert result["ok"] is False
    assert result["error"] == "memory_changed"
    assert has_draft()  # draft survives the refusal

    # Force applies over the concurrent change.
    result = apply_draft(force=True)
    assert result["ok"] is True
    assert "TypeScript" in _read_existing_memory()


@pytest.mark.asyncio
async def test_apply_draft_cutoff_keeps_newer_entries(isolated_home, monkeypatch):
    await _make_draft(monkeypatch)
    # A turn arrives while the draft is pending review.
    time.sleep(0.02)
    append_to_pool("s1", "dev", "审核期间的新轮次")

    result = apply_draft()
    assert result["ok"] is True
    assert result["pool_remaining"] == 1
    assert read_pool()[0]["content"] == "审核期间的新轮次"


@pytest.mark.asyncio
async def test_discard_draft_keeps_pool(isolated_home, monkeypatch):
    await _make_draft(monkeypatch)
    result = discard_draft()
    assert result["ok"] is True
    assert result["had_draft"] is True
    assert not has_draft()
    assert pool_count() == 1  # pool kept — can be re-distilled


def test_discard_without_draft(isolated_home):
    result = discard_draft()
    assert result["ok"] is True
    assert result["had_draft"] is False


def test_remove_memory_lines(isolated_home):
    _write_memory_raw = "## 技术栈\n- TypeScript\n- pnpm\n\n## 偏好\n- 暗色主题"
    paths.memory_index_path().write_text(_write_memory_raw, encoding="utf-8")

    removed = remove_memory_lines(["## 技术栈", "- TypeScript"])
    assert removed == 2
    mem = _read_existing_memory()
    assert "TypeScript" not in mem
    assert "- pnpm" in mem
    assert "暗色主题" in mem

    # no-op for empty / non-matching input
    assert remove_memory_lines([]) == 0
    assert remove_memory_lines(["不存在的内容"]) == 0
