"""GET /api/mods rows carry the per-mod ``compat`` counters (design §7.5
持续项), fed from the channel's in-memory stats. Real FastAPI stack, stub
channel — no broker, no node."""

from __future__ import annotations

import pytest

from ginno_runtime.mods import api as mods_api

pytestmark = pytest.mark.api


class CompatStubChannel:
    """The list_mods surface only: availability + a canned compat summary."""

    def __init__(self):
        self.compat: dict[str, dict] = {}
        self.mods_state: dict[str, dict] = {}

    def ensure_started(self):
        pass

    def availability(self):
        return {"status": "connected", "detail": "", "connected": True, "mods": []}

    def compat_summary(self):
        import copy

        return copy.deepcopy(self.compat)


@pytest.fixture
def stub_channel(monkeypatch):
    stub = CompatStubChannel()
    # api.py binds get_channel at import time — patch it there.
    monkeypatch.setattr(mods_api, "get_channel", lambda: stub)
    return stub


def test_mod_rows_carry_compat(client, stub_channel):
    # The row itself comes from the broker-reported lifecycle state.
    stub_channel.mods_state["token-weather"] = {"name": "token-weather", "status": "loaded"}
    stub_channel.compat["token-weather"] = {
        "events": {"turn.start": {"registered": True, "fired": 3, "failed": 1}},
        "ops": {"session.usage": {"called": 2, "ok": 2}},
        "unimplemented": {"mcp.call": 1},
    }
    r = client.get("/api/mods")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    rows = {row["name"]: row for row in body["mods"]}
    # A broker-known mod with counters gets its compat block verbatim.
    assert rows["token-weather"]["compat"]["events"]["turn.start"]["fired"] == 3
    assert rows["token-weather"]["compat"]["ops"]["session.usage"] == {"called": 2, "ok": 2}
    assert rows["token-weather"]["compat"]["unimplemented"] == {"mcp.call": 1}
    # Every row carries the key, empty when nothing was counted yet.
    assert all("compat" in row for row in body["mods"])
