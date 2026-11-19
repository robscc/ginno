"""API tests for GET /api/doctor (2026-11-19)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.api

SURFACES = {"mcp", "skills", "hooks", "settings", "runtime"}
LEVELS = {"ok", "warn", "error"}


def _ginno_home() -> Path:
    return Path(os.environ["GINNO_HOME"])


def test_doctor_endpoint_contract(client):
    r = client.get("/api/doctor")
    assert r.status_code == 200
    doc = r.json()
    assert set(doc) == {"ok", "ran_at", "summary", "checks"}
    assert doc["ok"] is True
    datetime.fromisoformat(doc["ran_at"])
    assert set(doc["summary"]) == {"error", "warn", "total"}
    assert isinstance(doc["checks"], list) and doc["checks"]
    ids = set()
    for c in doc["checks"]:
        assert set(c) == {"surface", "id", "level", "items"}
        assert c["surface"] in SURFACES
        assert c["level"] in LEVELS
        assert c["id"] not in ids  # stable, unique ids (frontend i18n keys)
        ids.add(c["id"])
        for item in c["items"]:
            assert set(item) == {"name", "detail"}
            assert isinstance(item["name"], str) and isinstance(item["detail"], str)
    assert {c["surface"] for c in doc["checks"]} == SURFACES
    assert doc["summary"]["total"] == len(doc["checks"])
    assert doc["summary"]["error"] == sum(1 for c in doc["checks"] if c["level"] == "error")
    assert doc["summary"]["warn"] == sum(1 for c in doc["checks"] if c["level"] == "warn")


def test_doctor_endpoint_surfaces_a_broken_mcp_config(client):
    mcp = _ginno_home() / "mcp" / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"dead": {"transport": "stdio"}}}))
    doc = client.get("/api/doctor").json()
    by_id = {c["id"]: c for c in doc["checks"]}
    assert by_id["mcp.json-parse"]["level"] == "error"
    assert any("dead" in i["name"] for i in by_id["mcp.json-parse"]["items"])
    assert doc["summary"]["error"] >= 1


def test_doctor_endpoint_reports_unknown_settings_key(client):
    sp = _ginno_home() / "settings.json"
    settings = json.loads(sp.read_text())
    settings["stale_remnant_key"] = True
    sp.write_text(json.dumps(settings))
    doc = client.get("/api/doctor").json()
    entry = next(c for c in doc["checks"] if c["id"] == "settings.unknown-keys")
    assert entry["level"] == "warn"
    assert [i["name"] for i in entry["items"]] == ["stale_remnant_key"]


def test_doctor_endpoint_never_500s_when_a_check_crashes(client, monkeypatch):
    from ginno_runtime import doctor as doctor_mod

    def boom(_ctx):
        raise RuntimeError("probe crash")

    monkeypatch.setattr(doctor_mod, "_check_skills_frontmatter", boom)
    r = client.get("/api/doctor")
    assert r.status_code == 200
    doc = r.json()
    assert doc["ok"] is True
    entry = next(c for c in doc["checks"] if c["id"] == "skills.frontmatter")
    assert entry["level"] == "error"
    assert "probe crash" in entry["items"][0]["detail"]
