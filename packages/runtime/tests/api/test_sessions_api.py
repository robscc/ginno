"""API integration tests for the /sessions endpoints."""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths, server
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


def _post_session(client, **overrides):
    body = {"project_slug": "default", "workspace": "/tmp/gw", "agent_id": "dev"}
    body.update(overrides)
    return client.post("/api/sessions", json=body)


def test_create_session_returns_meta_and_persists(client, patch_build_model):
    patch_build_model(script(text="ok"))
    r = _post_session(client)
    data = r.json()
    assert r.status_code == 200
    assert data["ok"] is True
    assert data["id"]
    assert data["agent_id"] == "dev"
    assert data["provider"] == "custom"  # nothing enabled -> fallthrough
    # persisted to the on-disk session index
    index = json.loads(paths.session_index_path("default").read_text())
    assert any(m["id"] == data["id"] for m in index)


def test_create_session_default_title_follows_agent(client, patch_build_model):
    patch_build_model(script(text="ok"))
    data = _post_session(client, agent_id="research").json()
    assert data["title"] == "Research Agent session"
    assert data["title_auto"] is True


def test_create_session_explicit_title(client, patch_build_model):
    patch_build_model(script(text="ok"))
    data = _post_session(client, title="My Chat").json()
    assert data["title"] == "My Chat"
    assert data["title_auto"] is False


def test_create_session_model_error_returns_ok_false(client, monkeypatch):
    def boom(*a, **k):
        raise ValueError("provider disabled")

    monkeypatch.setattr(server, "build_model", boom)
    data = _post_session(client).json()
    assert data["ok"] is False
    assert "provider disabled" in data["error"]


def test_list_sessions_prefers_disk_index(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client).json()["id"]
    listed = client.get("/api/sessions?project_slug=default").json()
    assert any(s["id"] == sid for s in listed)


def test_get_session(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client).json()["id"]
    got = client.get(f"/api/sessions/{sid}").json()
    # in-memory session shape keys the id as `session_id`
    assert got["session_id"] == sid


def test_patch_auto_title_follows_agent_switch(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client, agent_id="dev").json()["id"]
    # auto title follows the agent when switched
    r = client.patch(f"/api/sessions/{sid}", json={"agent_id": "research"}).json()
    assert r["session"]["title"] == "Research Agent session"
    assert r["session"]["title_auto"] is True


def test_patch_explicit_title_stops_auto_follow(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client, agent_id="dev").json()["id"]
    client.patch(f"/api/sessions/{sid}", json={"title": "Pinned"})
    # subsequent agent switch must NOT rename a manually-pinned title
    r = client.patch(f"/api/sessions/{sid}", json={"agent_id": "writer"}).json()
    assert r["session"]["title"] == "Pinned"
    assert r["session"]["title_auto"] is False


def test_delete_session_removes_index_and_checkpoint(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client).json()["id"]
    assert any(m["id"] == sid for m in json.loads(paths.session_index_path("default").read_text()))
    # a checkpoint file exists once the session has run at least once; create it
    cp = paths.project_sessions_dir("default") / f"{sid}.json"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text('{"checkpoints": []}')
    assert cp.exists()

    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # gone from the index
    assert not any(m["id"] == sid for m in json.loads(paths.session_index_path("default").read_text()))
    # checkpoint file removed
    assert not cp.exists()
    # no longer listed
    assert all(s["id"] != sid for s in client.get("/api/sessions").json())


def test_delete_session_unknown_is_ok(client):
    r = client.delete("/api/sessions/does-not-exist")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_rename_session_updates_title(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client).json()["id"]
    r = client.patch(f"/api/sessions/{sid}", json={"title": "My Renamed Session"}).json()
    assert r["session"]["title"] == "My Renamed Session"
    assert r["session"]["title_auto"] is False
    # persisted + reflected in the list
    listed = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert listed[sid]["title"] == "My Renamed Session"
