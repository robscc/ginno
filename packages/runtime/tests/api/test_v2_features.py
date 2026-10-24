"""API tests for the V2 backlog (search / paging / export / suggestions / distill)."""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


def _post(client, **overrides):
    body = {"project_slug": "default", "workspace": "/tmp/gw", "agent_id": "dev"}
    body.update(overrides)
    return client.post("/api/sessions", json=body).json()


def test_unified_search_matches_titles(client, patch_build_model):
    patch_build_model(script(text="ok"))
    _post(client, title="Trending digest")
    _post(client, title="Weekly notes")
    data = client.get("/api/search", params={"q": "trending"}).json()
    assert data["ok"] is True
    assert [s["title"] for s in data["sessions"]] == ["Trending digest"]
    assert data["messages"] == []  # index empty in a fresh HOME


def test_archived_scope_only_returns_archived(client, patch_build_model):
    patch_build_model(script(text="ok"))
    a = _post(client, title="Old gmail job")["id"]
    _post(client, title="Gmail live")
    client.post("/api/sessions/bulk", json={"action": "archive", "ids": [a]})
    data = client.get("/api/search", params={"q": "gmail", "scope": "archived"}).json()
    assert [s["id"] for s in data["sessions"]] == [a]


def test_sessions_page_reports_total_and_slices(client, patch_build_model):
    patch_build_model(script(text="ok"))
    for i in range(5):
        _post(client, title=f"S{i}")
    data = client.get("/api/sessions/page", params={"limit": 2, "offset": 1}).json()
    assert data["total"] >= 5
    assert len(data["sessions"]) == 2


def test_export_session_markdown(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post(client, title="Export me")["id"]
    r = client.get(f"/api/export/session/{sid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "# Export me" in r.text
    assert f"`{sid}`" in r.text


def test_suggestions_never_fire_for_automation(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post(client, title="Daily digest", workflow_id="wf-x")["id"]
    data = client.get(f"/api/sessions/{sid}/suggestions").json()
    assert data["ok"] is True and data["suggestions"] == []
    assert data.get("excluded") == "automation"


def test_suggestions_failure_rule_is_actionable(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post(client, title="Broken key")["id"]
    from ginno_runtime.session_meta import _session_meta_patch

    _session_meta_patch(
        "default", sid, {"last_error": {"message": "AuthenticationError: Error code: 401"}}
    )
    data = client.get(f"/api/sessions/{sid}/suggestions").json()
    hints = {s["id"]: s for s in data["suggestions"]}
    assert "failure.auth" in hints
    assert hints["failure.auth"]["action"]["type"] == "open_settings"


def test_distill_to_skill_writes_a_draft(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post(client, title="My repeatable task")["id"]
    r = client.post(f"/api/sessions/{sid}/distill", json={"kind": "skill"}).json()
    assert r["ok"] is True
    p = paths.global_skills_dir() / r["name"] / "SKILL.md"
    assert p.exists()
    body = p.read_text(encoding="utf-8")
    assert "description:" in body and "## 工作流" in body
    meta = {m["id"]: m for m in json.loads(paths.session_index_path("default").read_text())}
    assert meta[sid]["distilled_to"][0]["kind"] == "skill"


def test_pricing_cost_and_cache_saving(isolated_home):
    from ginno_runtime import paths as paths_mod
    from ginno_runtime import pricing

    paths_mod.settings_path().write_text(
        json.dumps({"pricing": {"default": {"input": 2.0, "output": 8.0, "cache_read": 0.2}}})
    )
    pricing._CACHE.update({"at": 0.0, "table": {}})
    cost, saving = pricing.cost_of(
        {
            "provider": "p",
            "model": "m",
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 400_000,
        }
    )
    # net input 0.6M * 2 + 0.1M * 8 + 0.4M * 0.2 = 1.2 + 0.8 + 0.08
    assert cost == pytest.approx(2.08, abs=1e-6)
    assert saving == pytest.approx(0.4 * 1.8, abs=1e-6)


def test_team_spec_normalization_clamps_members():
    from ginno_runtime.api.team_generate import _normalize_spec

    spec = _normalize_spec(
        {
            "name": "Squad",
            "members": [
                {"agent_id": "research", "display_name": "A", "role": "leader", "instructions": "x"},
                {"agent_id": "nope", "display_name": "B", "role": "leader", "instructions": "y"},
            ],
        }
    )
    assert spec["name"] == "Squad"
    assert [m["role"] for m in spec["members"]] == ["leader", "member"]
    assert spec["members"][1]["agent_id"] != "nope"
