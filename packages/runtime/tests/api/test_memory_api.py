"""API tests for memory endpoints (draft → review → apply lifecycle)."""

from __future__ import annotations

import time

import pytest

from ginno_runtime import paths
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


# ---- settings page: 已生效的记忆 / 队列 / 池 / 运行 / 用量 --------------------


def test_memory_status_projects_sections_and_budget(isolated_home, client):
    paths.memory_index_path().write_text(
        "## 环境\n- 本地文件\n- 无 Shell\n\n## 偏好\n- 中文\n", encoding="utf-8"
    )
    r = client.get("/api/memory/status").json()
    assert r["ok"] is True
    assert r["section_count"] == 2
    assert [s["title"] for s in r["sections"]] == ["环境", "偏好"]
    assert r["line_count"] == 3
    assert r["chars"] > 0
    # prompt view: the wrapped injection is at least the file itself
    assert r["injected_chars"] >= r["chars"]
    assert r["budget"] > 0


def test_draft_queue_endpoints(isolated_home, client, monkeypatch):
    append_to_pool("s", "dev", "偏好 TypeScript")
    _patch_model(monkeypatch, "## 偏好\n- TypeScript")

    created = client.post("/api/memory/summarize", json={}).json()
    draft_id = created["id"]
    assert draft_id

    listing = client.get("/api/memory/drafts").json()
    assert listing["ok"] is True
    assert [d["id"] for d in listing["drafts"]] == [draft_id]
    assert listing["drafts"][0]["status"] == "pending"

    detail = client.get(f"/api/memory/drafts/{draft_id}").json()
    assert detail["ok"] is True and "TypeScript" in detail["draft"]

    applied = client.post(f"/api/memory/drafts/{draft_id}/apply", json={}).json()
    assert applied["ok"] is True
    assert "TypeScript" in client.get("/api/memory").json()["content"]

    # resolved drafts stay in the queue as history
    rows = client.get("/api/memory/drafts").json()["drafts"]
    assert rows[0]["status"] == "applied"

    # unknown draft → 404
    assert client.get("/api/memory/drafts/nope").status_code == 404


def test_pool_browse_and_clear_requires_confirm(isolated_home, client):
    for i in range(3):
        append_to_pool("s", "dev", f"内容 {i}")

    page = client.get("/api/memory/pool?limit=2&offset=0").json()
    assert page["total"] == 3
    assert len(page["entries"]) == 2
    assert page["entries"][0]["excerpt"].startswith("内容")
    assert page["entries"][0]["chars"] > 0

    refused = client.request("DELETE", "/api/memory/pool", json={})
    assert refused.status_code == 400
    assert pool_count() == 3

    cleared = client.request("DELETE", "/api/memory/pool", json={"confirm": True}).json()
    assert cleared["ok"] is True and cleared["removed"] == 3
    assert pool_count() == 0


def test_memory_runs_and_usage(isolated_home, client, monkeypatch):
    from ginno_runtime import usage_store

    append_to_pool("s", "dev", "内容")
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    fake = ScriptedChatModel(
        scripts=[script(text="## A\n- x", usage={"input_tokens": 900, "output_tokens": 120, "total_tokens": 1020})]
    )
    monkeypatch.setattr("ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake)
    client.post("/api/memory/summarize", json={})

    runs = client.get("/api/memory/runs").json()["runs"]
    assert runs and runs[0]["status"] == "success"

    usage_rows = client.get("/api/memory/usage?days=7").json()
    assert usage_rows["calls"] == 1
    assert usage_rows["input_tokens"] == 900
    assert usage_rows["output_tokens"] == 120

    # The usage ledger is the same one Settings → 用量统计 reads.
    today = time.strftime("%Y-%m-%d", time.localtime())
    assert any(e.get("source") == "memory" for e in usage_store.load_day(today))


def test_put_memory_and_remove_section(isolated_home, client):
    wrote = client.put(
        "/api/memory", json={"content": "## A\n- one\n\n## B\n- two"}
    ).json()
    assert wrote["ok"] is True
    assert wrote["status"]["section_count"] == 2

    removed = client.post(
        "/api/memory/sections/remove", json={"lines": ["## B", "- two"]}
    ).json()
    assert removed["ok"] is True and removed["removed"] == 2
    content = client.get("/api/memory").json()["content"]
    assert "two" not in content and "one" in content


def test_expired_queue_cleanup(isolated_home, client):
    from ginno_runtime.memory import summarize as S

    did = f"{int(time.time())}-old"
    S._atomic_write(S._draft_paths(did)[0], "## A\n- x")
    S._save_meta(did, {"id": did, "created_at": time.time(), "status": S.EXPIRED, "chars": 8})

    r = client.request("DELETE", "/api/memory/drafts/expired").json()
    assert r["ok"] is True and did in r["removed"]
    assert client.get("/api/memory/drafts").json()["drafts"] == []


# ---- P2: retry / trend / split --------------------------------------------


def test_retry_run_endpoint(isolated_home, client, monkeypatch):
    from ginno_runtime.memory import record_run

    append_to_pool("s", "dev", "内容")
    _patch_model(monkeypatch, "## A\n- x")
    record_run(run_id="r-fail", trigger="auto", status="failed", reason="summarization_failed")

    r = client.post("/api/memory/runs/r-fail/retry").json()
    assert r["ok"] is True and r["retried_from"] == "r-fail"
    assert client.get("/api/memory").json()["draft_pending"] is True

    # A pending draft is reused instead of burning another call.
    again = client.post("/api/memory/runs/r-fail/retry").json()
    assert again.get("draft_exists") is True

    # Throttle state is exposed for the page.
    assert "throttle" in client.get("/api/memory").json()


def test_trend_endpoint(isolated_home, client):
    from ginno_runtime.memory import record_snapshot

    # The startup sweep already wrote today's snapshot; a manual one replaces it
    # instead of appending a second row for the same day.
    record_snapshot()
    r = client.get("/api/memory/trend?days=7").json()
    assert r["ok"] is True and len(r["trend"]) == 1
    row = r["trend"][0]
    assert {"date", "pool", "memory_chars", "budget", "drafts_pending"} <= set(row)


def test_split_draft_endpoint(isolated_home, client, monkeypatch):
    append_to_pool("s", "dev", "内容")
    _patch_model(monkeypatch, "## A\n- alpha\n\n## B\n- beta")
    did = client.post("/api/memory/summarize", json={}).json()["id"]

    r = client.post(f"/api/memory/drafts/{did}/split", json={"keep": ["## B"]}).json()
    assert r["ok"] is True and r["id"] != did
    assert "beta" in (r["draft"] or "") and "alpha" not in (r["draft"] or "")

    rows = {d["id"]: d["status"] for d in client.get("/api/memory/drafts").json()["drafts"]}
    assert rows[did] == "discarded"
    assert rows[r["id"]] == "pending"

    # Empty selection is a 400-shaped error from the handler.
    bad = client.post(f"/api/memory/drafts/{r['id']}/split", json={"keep": []})
    assert bad.status_code == 400
