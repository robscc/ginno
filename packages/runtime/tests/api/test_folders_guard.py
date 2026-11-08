"""API guard for runtime-home folder registration (privileged-session design).

POST /api/folders and PATCH /api/folders/{id}: entries inside the runtime
data home come back read-only regardless of the requested access tier, and
the clamp is visible via ``forced_ro`` so the UI can badge it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.api


def _home_dir(client: TestClient) -> str:
    # GET /api/folders reports the resolved runtime home (never guess "~").
    return client.get("/api/folders").json()["runtime_home"]


def test_create_runtime_home_entry_forced_ro(client: TestClient):
    home = _home_dir(client)
    r = client.post("/api/folders", json={"path": home, "access": "rw"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["folder"]["access"] == "ro"
    assert body["folder"]["forced_ro"] is True


def test_patch_runtime_home_entry_cannot_escalate(client: TestClient):
    home = _home_dir(client)
    f = client.post("/api/folders", json={"path": home}).json()["folder"]
    r = client.patch(f"/api/folders/{f['id']}", json={"access": "rw"})
    body = r.json()
    assert body["ok"] is True
    assert body["folder"]["access"] == "ro"
    assert body["folder"]["forced_ro"] is True


def test_probe_flags_runtime_home(client: TestClient):
    home = _home_dir(client)
    r = client.post("/api/folders/probe", json={"path": home})
    assert r.json()["runtime_home"] is True


def test_plain_folder_outside_home_unchanged(client: TestClient, tmp_path):
    # NOTE: the isolated-home fixture reuses this test's tmp_path as
    # $GINNO_HOME, so "outside" must escape tmp_path entirely (its parent).
    d = tmp_path.parent / "folders_guard_outside"
    d.mkdir(parents=True, exist_ok=True)
    r = client.post("/api/folders", json={"path": str(d), "access": "rw"})
    body = r.json()
    assert body["folder"]["access"] == "rw"
    assert body["folder"].get("forced_ro") is False
