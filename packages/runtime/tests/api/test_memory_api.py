"""API tests for memory endpoints (draft → review → apply lifecycle)."""

from __future__ import annotations

import pytest

from ginno_runtime.memory.pool import append_to_pool, pool_count

pytestmark = pytest.mark.api


def _patch_model(monkeypatch, text: str) -> None:
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    fake = ScriptedChatModel(scripts=[script(text=text)])
    monkeypatch.setattr(
        "ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake
    )


def test_get_memory_empty(isolated_home, client):
    r = client.get("/api/memory").json()
    assert r["ok"] is True
    assert r["pool_count"] == 0
    assert r["draft_pending"] is False
    assert r["kb_usable"] is False
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


def test_draft_lifecycle(isolated_home, client, monkeypatch):
    append_to_pool("s", "dev", "偏好使用 TypeScript")
    append_to_pool("s", "dev", "使用 pnpm")
    _patch_model(monkeypatch, "## 偏好\n- TypeScript\n- pnpm")

    # summarize now produces a DRAFT — MEMORY.md stays untouched
    r = client.post("/api/memory/summarize", json={}).json()
    assert r["ok"] is True
    assert r["pool_entries"] == 2
    assert "TypeScript" in r["draft"]
    mem = client.get("/api/memory").json()
    assert mem["draft_pending"] is True
    assert pool_count() == 2
    assert "TypeScript" not in mem["content"]

    # GET draft recovers the pending review payload
    d = client.get("/api/memory/draft").json()
    assert d["ok"] is True
    assert d["draft"] is not None
    assert isinstance(d["diff"], str)

    # apply writes MEMORY.md + clears the pool
    a = client.post("/api/memory/draft/apply", json={}).json()
    assert a["ok"] is True
    mem = client.get("/api/memory").json()
    assert "TypeScript" in mem["content"]
    assert mem["draft_pending"] is False
    assert pool_count() == 0

    # apply without a draft errors cleanly
    a2 = client.post("/api/memory/draft/apply", json={}).json()
    assert a2["ok"] is False
    assert a2["error"] == "no_draft"


def test_draft_discard_keeps_pool(isolated_home, client, monkeypatch):
    append_to_pool("s", "dev", "内容")
    _patch_model(monkeypatch, "## A\n- x")
    client.post("/api/memory/summarize", json={})
    r = client.post("/api/memory/draft/discard").json()
    assert r["ok"] is True
    assert client.get("/api/memory").json()["draft_pending"] is False
    assert pool_count() == 1


def test_draft_get_none(isolated_home, client):
    r = client.get("/api/memory/draft").json()
    assert r["ok"] is True
    assert r["draft"] is None
