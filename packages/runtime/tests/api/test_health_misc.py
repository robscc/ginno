"""API integration tests for health, settings, and misc endpoints."""

from __future__ import annotations

import pytest

from ginno_runtime import server

pytestmark = pytest.mark.api


def test_health(client):
    r = client.get("/health").json()
    assert r["ok"] is True
    assert r["version"] == "0.1.0"


def test_settings_get_seeded(client):
    settings = client.get("/settings").json()
    assert "providers" in settings
    assert "permissions" in settings


def test_settings_put_roundtrip(client):
    payload = {"providers": {}, "custom_key": "value"}
    assert client.put("/settings", json=payload).json()["ok"] is True
    assert client.get("/settings").json()["custom_key"] == "value"


def test_unknown_route_falls_through_to_ui_or_404(client):
    # The trailing catch-all either serves the bundled SPA (so deep links like
    # /kb resolve via index.html -> 200) when the UI is built, or 404s when it
    # isn't (CI / runtime-only dev). Both are correct; the guarantee is that an
    # unknown path never 500s and never collides with a real API endpoint.
    r = client.get("/this/route/does/not/exist")
    if server.WEB_OUT is not None:
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
    else:
        assert r.status_code == 404


def test_skills_body_unknown_returns_empty(client):
    r = client.get("/skills/nope/body").json()
    assert r["ok"] is False
    assert r["body"] == ""
