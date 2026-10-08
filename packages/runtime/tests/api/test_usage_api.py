"""API tests for /api/usage/* and the logged session usage (design §5)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

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


def test_grid_endpoint(client):
    """点格图：连续日历 × 12 个 2h 桶，cell = [gross, net, out, cache, calls]"""
    _seed(input_tokens=1000, output_tokens=100, cache_read_tokens=600)
    data = client.get("/api/usage/grid").json()
    assert data["ok"]
    assert len(data["days"]) == len(data["grid"]) == 30
    assert data["days"][-1] == time.strftime("%Y-%m-%d", time.localtime())
    row = data["grid"][-1]
    assert len(row) == 12
    slot = time.localtime().tm_hour // 2
    cell = row[slot]
    assert cell[4] == 1                       # requests
    assert cell[0] == 1100                    # gross = in + out
    assert cell[1] == 1000 + 100 - 600        # net = (in - cache) + out
    assert cell[2] == 100 and cell[3] == 600  # out / cache
    assert row[(slot + 6) % 12] == [0, 0, 0, 0, 0]


def test_grid_endpoint_from_to_and_missing_days(client):
    today = datetime.now()
    day0 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    day1 = today.strftime("%Y-%m-%d")
    _seed()
    data = client.get(f"/api/usage/grid?from={day0}&to={day1}").json()
    assert data["days"] == [day0, day1]
    # 没有 jsonl 文件的昨天照样占一列，整列全 0，且无 have/have_file 标记
    assert data["grid"][0] == [[0, 0, 0, 0, 0]] * 12
    assert "have" not in repr(data)


def test_grid_endpoint_clamps_to_retention(client):
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    data = client.get(f"/api/usage/grid?from={old}").json()
    assert len(data["days"]) == usage_store.RETENTION_DAYS
    assert len(data["grid"]) == usage_store.RETENTION_DAYS


def test_hourly_endpoint_removed(client):
    """/api/usage/hourly 没有调用方后被删除（design §7）。注意不能断言 404：
    server 末尾的 SPA catch-all 会把任何未知路径回成 index.html。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/usage/grid" in paths
    assert "/api/usage/hourly" not in paths


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
    # langchain 1.x shape: input_tokens is already the WHOLE prompt
    # (non-cached 60 + cache_read 40); extraction passes it through.
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
    # whole-prompt input passes through unchanged
    assert row["input_tokens"] == 100
    assert row["cache_read_tokens"] == 40
    assert row["provider"] == "custom"  # nothing enabled -> fallthrough provider

    # session usage endpoint now sees the logged total
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["usage"]["input_tokens"] == 100
    assert data["usage"]["calls"] == 1

    # and it survives an in-memory reset (simulated runtime restart)
    from ginno_runtime import server
    server._USAGE_BY_SESSION.clear()
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["usage"]["input_tokens"] == 100
