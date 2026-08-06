"""WebSocket E2E: structured-output tools (render_widget, attach_ref).

These tools are surfaced from the agent node's complete tool_calls in `updates`
mode — not as ordinary tool bubbles — so the WS layer emits widget.emit /
ref.emit and never a tool.start/tool.end for them.
"""

from __future__ import annotations

import pytest

from conftest import event_names, events_of
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def test_render_widget_emits_widget_not_tool_bubble(create_session, ws_conv):
    widget = {"kind": "stat_list", "data": {"title": "PRs", "items": [{"label": "open", "value": "3"}]}}
    model = [
        script(tool_calls=[script_tool_call("render_widget", widget)]),
        script(text="here is the summary"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("show PR status")
        events = conv.recv_until("message.end", "error")

    widgets = events_of(events, "widget.emit")
    assert len(widgets) == 1
    assert widgets[0]["kind"] == "stat_list"
    assert widgets[0]["data"]["title"] == "PRs"
    # render_widget is silent: no ordinary tool bubble
    tool_starts = events_of(events, "tool.start")
    assert all(t.get("name") != "render_widget" for t in tool_starts)


def test_render_chart_widget_emits_spec(create_session, ws_conv):
    chart = {
        "kind": "chart",
        "data": {
            "type": "bar",
            "title": "commits",
            "x": "month",
            "y": "count",
            "data": [{"month": "Jan", "count": 12}, {"month": "Feb", "count": 19}],
        },
    }
    model = [
        script(tool_calls=[script_tool_call("render_widget", chart)]),
        script(text="Feb is the peak"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("chart the commits")
        events = conv.recv_until("message.end", "error")

    widgets = events_of(events, "widget.emit")
    assert len(widgets) == 1
    assert widgets[0]["kind"] == "chart"
    assert widgets[0]["data"]["type"] == "bar"
    assert widgets[0]["data"]["data"][1] == {"month": "Feb", "count": 19}
    tool_starts = events_of(events, "tool.start")
    assert all(t.get("name") != "render_widget" for t in tool_starts)


def test_attach_ref_emits_ref_and_registers_artifact(create_session, ws_conv, client):
    model = [
        script(tool_calls=[script_tool_call("attach_ref", {"kind": "file", "name": "notes.md", "ref_id": "/v/notes.md"})]),
        script(text="attached"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("attach the notes")
        events = conv.recv_until("message.end", "error")

    refs = events_of(events, "ref.emit")
    assert len(refs) == 1
    assert refs[0]["kind"] == "file"
    assert refs[0]["name"] == "notes.md"
    # the file ref is auto-registered as an artifact
    arts = client.get("/api/artifacts?project_slug=default").json()
    assert any(a["name"] == "notes.md" and a["kind"] == "file" for a in arts)


def test_attach_ref_no_tool_bubble(create_session, ws_conv):
    model = [
        script(tool_calls=[script_tool_call("attach_ref", {"kind": "doc", "name": "spec.md"})]),
        script(text="done"),
    ]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("attach")
        events = conv.recv_until("message.end", "error")
    assert all(t.get("name") != "attach_ref" for t in events_of(events, "tool.start"))
    assert "ref.emit" in event_names(events)
