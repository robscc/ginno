"""API tests for memory endpoints (GET /memory, POST /memory/summarize)."""

from __future__ import annotations

import pytest

from ginno_runtime.memory.pool import append_to_pool, pool_count

pytestmark = pytest.mark.api


def test_get_memory_empty(isolated_home, client):
    r = client.get("/api/memory").json()
    assert r["ok"] is True
    assert r["pool_count"] == 0
    # default boilerplate or empty
    assert "Ginno Memory" in r["content"] or r["content"] == ""


def test_get_memory_with_pool(isolated_home, client):
    append_to_pool("s", "dev", "some content")
    r = client.get("/api/memory").json()
    assert r["pool_count"] == 1


def test_summarize_empty_pool(isolated_home, client):
    r = client.post("/api/memory/summarize", json={}).json()
    assert r["ok"] is True
    assert r["pool_entries"] == 0


def test_summarize_with_pool_and_fake_model(isolated_home, client, monkeypatch):
    append_to_pool("s", "dev", "偏好使用 TypeScript")
    append_to_pool("s", "dev", "使用 pnpm")

    fake_summary = "## 偏好\n- TypeScript\n- pnpm"
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    fake_model = ScriptedChatModel(scripts=[script(text=fake_summary)])
    monkeypatch.setattr("ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake_model)

    r = client.post("/api/memory/summarize", json={}).json()
    assert r["ok"] is True
    assert r["pool_entries"] == 2
    assert pool_count() == 0  # cleared after summarize

    # MEMORY.md should now have the summary
    mem = client.get("/api/memory").json()
    assert "TypeScript" in mem["content"]
