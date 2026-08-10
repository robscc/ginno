"""API tests for /api/usage/* and the logged session usage (design §5)."""

from __future__ import annotations

import pytest

from ginno_runtime import usage_store
from ginno_runtime.testing.fake_model import script

from conftest import events_of

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def _fresh_store_cache():
    usage_store.reset_cache()
    yield
    usage_store.reset_cache()


def _seed(session_id="sess-a", provider="anthropic", model="claude-x", source="chat", **kw):
    usage_store.record(
        input_tokens=kw.get("input_tokens", 1000),
        output_tokens=kw.get("output_tokens", 100),
        cache_read_tokens=kw.get("cache_read_tokens", 600),
        cache_creation_tokens=kw.get("cache_creation_tokens", 0),
        provider=provider, model=model, source=source,
        session_id=session_id, project_slug="default",
    )


def test_overview_endpoint(client):
    _seed()
    _seed(provider="custom", model="deepseek-chat", session_id="sess-b",
          input_tokens=500, cache_read_tokens=0)
    r = client.get("/api/usage/overview?days=30")
    data = r.json()
    assert r.status_code == 200 and data["ok"]
    assert data["totals"]["input_tokens"] == 1500
    assert data["totals"]["calls"] == 2
    assert data["sessions_active"] == 2
    assert data["providers"][0]["provider"] == "anthropic"
    assert data["models"][0]["model"] == "claude-x"
    assert len(data["daily"]) == 30


def test_overview_days_clamped(client):
    _seed()
    assert client.get("/api/usage/overview?days=0").json()["window"]["days"] == 1
    assert client.get("/api/usage/overview?days=9999").json()["window"]["days"] == usage_store.RETENTION_DAYS


def test_overview_sources_breakdown(client):
    """Overview splits usage by source (chat vs workflow, design §3.6) while
    totals stay whole-account; sorted by total tokens desc."""
    _seed(source="chat", input_tokens=1000, output_tokens=100)
    _seed(source="workflow", session_id=None, input_tokens=300, output_tokens=50)
    _seed(source="workflow", session_id=None, input_tokens=200, output_tokens=20)
    data = client.get("/api/usage/overview?days=7").json()
    srcs = {s["source"]: s for s in data["sources"]}
    assert srcs["chat"]["calls"] == 1
    assert srcs["chat"]["input_tokens"] == 1000
    assert srcs["workflow"]["calls"] == 2
    assert srcs["workflow"]["input_tokens"] == 500
    assert data["sources"][0]["source"] == "chat"  # biggest first
    assert data["totals"]["input_tokens"] == 1500  # whole-account unchanged


def test_hourly_endpoint(client):
    _seed()
    data = client.get("/api/usage/hourly").json()
    assert data["ok"] and len(data["hours"]) == 24
    assert sum(b["calls"] for b in data["hours"]) == 1


def test_sessions_endpoint_joins_meta_and_placeholder(client):
    _seed(session_id="ghost-session")  # no session meta -> deleted placeholder
    data = client.get("/api/usage/sessions").json()
    assert data["ok"]
    rows = data["sessions"]
    assert rows[0]["session_id"] == "ghost-session"
    assert rows[0]["deleted"] is True
    assert rows[0]["title"].startswith("(已删除)")


def test_requests_endpoint_filters_and_paginates(client):
    for i in range(6):
        _seed(session_id="s", provider="anthropic" if i % 2 else "openai")
    data = client.get("/api/usage/requests?provider=anthropic").json()
    assert data["ok"] and data["total"] == 3
    page = client.get("/api/usage/requests?page=1&page_size=4").json()
    assert len(page["rows"]) == 4 and page["total"] == 6
    assert client.get("/api/usage/requests?source=goal").json()["total"] == 0


def test_session_usage_prefers_log(client):
    _seed(session_id="sess-log", input_tokens=4000, cache_read_tokens=1000)
    data = client.get("/api/sessions/sess-log/usage").json()
    assert data["ok"] and data["usage"]["input_tokens"] == 4000
    assert data["usage"]["cache_hit_ratio"] == 0.25
    # unknown session with no log -> usage None
    assert client.get("/api/sessions/never-seen/usage").json()["usage"] is None


def test_turn_is_recorded_end_to_end(create_session, ws_conv, client):
    """A real turn through the graph writes a usage row with the session's
    provider/model and source=chat, visible via the usage APIs."""
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
             "input_token_details": {"cache_read": 40, "cache_creation": 0}}
    sid = create_session([script(text="done", usage=usage)], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        events = conv.recv_until("message.end", "error")
    assert events_of(events, "usage"), "expected a usage event for the turn"

    req = client.get(f"/api/usage/requests?session_id={sid}").json()
    assert req["total"] == 1
    row = req["rows"][0]
    assert row["session_id"] == sid
    assert row["source"] == "chat"
    # normalized whole-prompt input: 100 + 40 + 0
    assert row["input_tokens"] == 140
    assert row["cache_read_tokens"] == 40
    assert row["provider"] == "custom"  # nothing enabled -> fallthrough provider

    # session usage endpoint now sees the logged total
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["usage"]["input_tokens"] == 140
    assert data["usage"]["calls"] == 1

    # and it survives an in-memory reset (simulated runtime restart)
    from ginno_runtime import server
    server._USAGE_BY_SESSION.clear()
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["usage"]["input_tokens"] == 140
