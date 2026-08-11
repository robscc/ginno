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

    monkeypatch.setattr("ginno_runtime.api.sessions.build_model", boom)
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


def test_patch_agent_switch_keeps_auto_title(client, patch_build_model):
    patch_build_model(script(text="ok"))
    data = _post_session(client, agent_id="dev").json()
    # auto titles now come from the first user message (stream side); an
    # agent switch must no longer rename.
    r = client.patch(f"/api/sessions/{data['id']}", json={"agent_id": "research"}).json()
    assert r["session"]["title"] == data["title"]
    assert r["session"]["title_auto"] is True


def test_patch_explicit_title_stops_auto_follow(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client, agent_id="dev").json()["id"]
    client.patch(f"/api/sessions/{sid}", json={"title": "Pinned"})
    # subsequent agent switch must NOT rename a manually-pinned title
    r = client.patch(f"/api/sessions/{sid}", json={"agent_id": "writer"}).json()
    assert r["session"]["title"] == "Pinned"
    assert r["session"]["title_auto"] is False


def test_patch_model_switch_updates_meta_and_drops_graph(client, patch_build_model):
    patch_build_model(script(text="ok"))
    sid = _post_session(client).json()["id"]
    from ginno_runtime.server_shared import _SESSIONS

    assert sid in _SESSIONS
    r = client.patch(f"/api/sessions/{sid}", json={"model": "other-model"}).json()
    assert r["ok"] is True
    assert r["session"]["model"] == "other-model"
    # in-memory graph dropped → rebuilt with the new model on next WS connect
    assert sid not in _SESSIONS


def test_patch_model_switch_rejects_invalid(client, monkeypatch):
    def fake(provider, model=None):
        if model == "nope":
            raise ValueError("unknown model")
        return object()

    monkeypatch.setattr("ginno_runtime.api.sessions.build_model", fake)
    sid = _post_session(client).json()["id"]
    r = client.patch(f"/api/sessions/{sid}", json={"model": "nope"}).json()
    assert r["ok"] is False
    assert "unknown model" in r["error"]


def test_touch_session_title_first_message_then_bump(monkeypatch):
    import asyncio

    from ginno_runtime.api import stream as stream_mod

    calls: dict = {}
    monkeypatch.setattr(
        stream_mod, "_find_meta", lambda sid: ({"id": sid, "title_auto": True}, "default")
    )

    def fake_patch(slug, sid, patch):
        calls["patch"] = patch
        return {"id": sid, **patch}

    monkeypatch.setattr(stream_mod, "_session_meta_patch", fake_patch)
    events: list = []

    async def fake_push(sid, kind, payload, turn_id):
        events.append((kind, payload))

    monkeypatch.setattr(stream_mod, "_push_session_event", fake_push)

    session: dict = {}
    asyncio.run(stream_mod._touch_session_title("default", "s1", session, "hello\nworld", "t1"))
    assert calls["patch"] == {"title": "hello world", "title_auto": False}
    assert events == [("session_title", {"title": "hello world"})]
    assert session["title_auto"] is False

    # subsequent turn: title sticks, only the `updated` bump (empty patch)
    monkeypatch.setattr(
        stream_mod, "_find_meta", lambda sid: ({"id": sid, "title_auto": False}, "default")
    )
    calls.clear()
    events.clear()
    asyncio.run(stream_mod._touch_session_title("default", "s1", session, "again", "t2"))
    assert calls["patch"] == {}
    assert events == []


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
