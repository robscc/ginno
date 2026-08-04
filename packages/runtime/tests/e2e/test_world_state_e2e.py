"""E2E: WorldState change announcements over the real WS flow (plan C1-C3).

Covers the chip display-level table inputs (what the backend emits; the
frontend decides visibility — date-only changes are silent in the UI but the
event still arrives, per spec), agent switch / prompt edit / permission
toggle / date rollover / mcp drift, plus D2 usage events.
"""

from __future__ import annotations

import json

import pytest
from conftest import events_of
from langchain_core.messages import AIMessage

from ginno_runtime import agents as agents_reg
from ginno_runtime import paths
from ginno_runtime.world_state import UPDATE_MSG_PREFIX, world_state_path

pytestmark = pytest.mark.e2e


def _ai(text="ok", usage=None):
    kw = {"id": None}
    if usage:
        kw["usage_metadata"] = usage
    return AIMessage(content=text, **{k: v for k, v in kw.items() if v is not None})


def _history_context_blocks(client, sid):
    r = client.get(f"/api/sessions/{sid}/history").json()
    out = []
    for entry in r.get("messages", []):
        if entry.get("role") == "system":
            for b in entry.get("blocks", []):
                if b.get("kind") == "context":
                    out.append(b["text"])
    return out


def test_first_turn_no_announcement(create_session, ws_conv, client):
    from conftest import script

    sid = create_session([script(text="你好")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        events = conv.recv_until("message.end", "error")
    # baseline recorded silently — world already conveyed by the system layer
    assert events_of(events, "context.updated") == []
    assert _history_context_blocks(client, sid) == []
    assert world_state_path("default", sid).exists()


def test_agent_prompt_edit_announced(create_session, ws_conv, client):
    from conftest import script

    sid = create_session([script(text="你好"), script(text="好的")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        conv.recv_until("message.end", "error")

    dev = agents_reg.get_agent("dev")
    new_prompt = (dev.system_prompt or "") + "\nAlways be brief."
    agents_reg.update_agent("dev", {"system_prompt": new_prompt})

    with ws_conv(sid) as conv:
        conv.invoke("next")
        events = conv.recv_until("message.end", "error")

    ups = events_of(events, "context.updated")
    assert len(ups) == 1
    assert {c["section"] for c in ups[0]["changes"]} == {"agent"}
    assert "角色设定" in ups[0]["changes"][0]["summary"]
    # the update message landed in history as a system context block (chip row)
    blocks = _history_context_blocks(client, sid)
    assert any(b.startswith(UPDATE_MSG_PREFIX) and "角色设定" in b for b in blocks)


def test_agent_switch_announced(create_session, ws_conv, client):
    from conftest import script

    sid = create_session([script(text="你好"), script(text="切换了")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        conv.recv_until("message.end", "error")
    with ws_conv(sid) as conv:
        conv.invoke("do research", agent_id="research")
        events = conv.recv_until("message.end", "error")
    ups = events_of(events, "context.updated")
    assert len(ups) == 1
    summary = ups[0]["changes"][0]["summary"]
    assert "切换" in summary and "Research Agent" in summary


def test_permission_toggle_announced(create_session, ws_conv, client, isolated_home):
    from conftest import script

    sid = create_session([script(text="你好"), script(text="好")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        conv.recv_until("message.end", "error")

    sp = paths.settings_path()
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = not s.get("bypass_permissions", True)
    sp.write_text(json.dumps(s))

    with ws_conv(sid) as conv:
        conv.invoke("again")
        events = conv.recv_until("message.end", "error")
    ups = events_of(events, "context.updated")
    assert len(ups) == 1
    assert ups[0]["changes"][0]["section"] == "permissions"


def test_date_rollover_announced_once(create_session, ws_conv, client):
    from conftest import script

    sid = create_session([script(text="你好"), script(text="新的一天")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        conv.recv_until("message.end", "error")

    # simulate a midnight rollover: rewind the stored baseline date
    p = world_state_path("default", sid)
    data = json.loads(p.read_text())
    data["snapshot"]["environment"]["date"] = "1999-12-31"
    data["snapshot"]["environment"]["weekday"] = "星期五"
    p.write_text(json.dumps(data))

    with ws_conv(sid) as conv:
        conv.invoke("next day")
        events = conv.recv_until("message.end", "error")
    ups = events_of(events, "context.updated")
    assert len(ups) == 1
    assert ups[0]["changes"][0]["section"] == "environment"  # UI renders this silent
    blocks = _history_context_blocks(client, sid)
    assert any("日期已更新" in b for b in blocks)

    # third turn: nothing changed → no further announcements
    with ws_conv(sid) as conv:
        conv.invoke("and another")
        events = conv.recv_until("message.end", "error")
    assert events_of(events, "context.updated") == []


def test_mcp_drift_from_baseline_announced(create_session, ws_conv, client):
    """Baseline says the session had MCP tools that are gone now (e.g. after a
    failed reload) → the model/user are told, not left guessing."""
    from conftest import script

    sid = create_session([script(text="你好"), script(text="好")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        conv.recv_until("message.end", "error")

    p = world_state_path("default", sid)
    data = json.loads(p.read_text())
    data["snapshot"]["mcp"] = {"count": 3, "hash": "stale"}
    p.write_text(json.dumps(data))

    with ws_conv(sid) as conv:
        conv.invoke("again")
        events = conv.recv_until("message.end", "error")
    ups = events_of(events, "context.updated")
    assert len(ups) == 1
    assert ups[0]["changes"][0]["section"] == "mcp"
    assert "3 → 0" in ups[0]["changes"][0]["summary"]


def test_usage_events_accumulate(create_session, ws_conv):
    usage1 = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
              "input_token_details": {"cache_read": 40, "cache_creation": 0}}
    usage2 = {"input_tokens": 150, "output_tokens": 12, "total_tokens": 162,
              "input_token_details": {"cache_read": 90, "cache_creation": 5}}
    sid = create_session([_ai("一", usage1), _ai("二", usage2)], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("t1")
        e1 = conv.recv_until("message.end", "error")
        conv.invoke("t2")
        e2 = conv.recv_until("message.end", "error")

    u1 = events_of(e1, "usage")
    assert len(u1) == 1 and u1[0]["turn"]["input_tokens"] == 100
    assert u1[0]["session"]["calls"] == 1
    assert u1[0]["session"]["cache_read_tokens"] == 40

    u2 = events_of(e2, "usage")
    assert len(u2) == 1 and u2[0]["turn"]["input_tokens"] == 150
    assert u2[0]["session"]["calls"] == 2
    assert u2[0]["session"]["input_tokens"] == 250
    assert u2[0]["session"]["cache_read_tokens"] == 130
    assert u2[0]["cache_hit_ratio"] == round(130 / 250, 4)
