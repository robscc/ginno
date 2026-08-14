"""Browser REST + workflow BrowserNode."""

from __future__ import annotations

import pytest

from ginno_runtime.browser import reset_supervisor
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows.nodes import get_node, known_types

pytestmark = pytest.mark.api


def test_browser_state_empty(client):
    r = client.get("/api/browser/state")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["spaces"] == []
    # Tests force FakeEngine; production would report chrome or a real error.
    assert data.get("engine") in ("fake", "chrome", "idle")


def test_browser_eval_login_wall_and_handoff(client):
    reset_supervisor()
    created = client.post(
        "/api/browser/spaces", json={"name": "check inbox", "session_id": "s1"}
    ).json()
    assert created["ok"] is True
    assert created["space"]["owner"] == "agent"

    nav = client.post(
        "/api/browser/spaces/check inbox/navigate",
        json={"url": "ginno://login-wall"},
    ).json()
    assert nav["ok"] is True

    ev = client.post(
        "/api/browser/eval",
        json={
            "space": "check inbox",
            "session_id": "s1",
            "code": "await handOffTaskSpace('need login')",
        },
    ).json()
    assert ev.get("interrupt") == "handoff"
    state = client.get("/api/browser/state").json()
    rec = next(s for s in state["spaces"] if s["name"] == "check inbox")
    assert rec["owner"] == "agentDelegatedToUser"
    assert state["waiting_human"] is True

    locked = client.post(
        "/api/browser/eval",
        json={"space": "check inbox", "code": "await click('@3')"},
    ).json()
    assert locked["ok"] is False
    assert "hard-stop" in (locked.get("error") or "") or "Delegated" in (locked.get("error") or "")

    taken = client.post("/api/browser/spaces/check inbox/takeover").json()
    assert taken["ok"] is True
    assert taken["space"]["owner"] == "agent"


def test_browser_complete_and_screenshot_and_dock(client):
    reset_supervisor()
    created = client.post("/api/browser/spaces", json={"name": "shot-me"}).json()
    assert created["ok"] is True
    shot = client.post("/api/browser/spaces/shot-me/screenshot", json={}).json()
    assert shot["ok"] is True
    assert shot.get("path")
    dock = client.post(
        "/api/browser/dock", json={"x": 10, "y": 20, "width": 400, "height": 300}
    ).json()
    assert dock["ok"] is True
    frame = client.get("/api/browser/spaces/shot-me/frame")
    assert frame.status_code == 200
    assert frame.headers.get("content-type", "").startswith("image/")
    hover = client.post(
        "/api/browser/spaces/shot-me/input",
        json={"type": "mouseMoved", "x": 12, "y": 12},
    ).json()
    assert hover["ok"] is False
    clicked = client.post(
        "/api/browser/spaces/shot-me/input",
        json={"type": "mousePressed", "x": 12, "y": 12},
    ).json()
    assert clicked["ok"] is True
    assert clicked.get("handoff") is True
    done = client.post("/api/browser/spaces/shot-me/complete", json={"keep": False}).json()
    assert done["ok"] is True
    assert done.get("kept") is False
    missing = client.get("/api/browser/spaces/shot-me").json()
    assert missing["ok"] is False


def test_browser_import_chrome_status(client):
    st = client.get("/api/browser/import-chrome").json()
    assert st["ok"] is True
    assert "profiles" in st
    assert "imported" in st
    # Isolated home has no Chrome user-data; POST should refuse cleanly.
    posted = client.post("/api/browser/import-chrome", json={"profile": "Default"}).json()
    assert posted["ok"] is False
    assert posted.get("error")


def test_browser_shared_space_lock_via_rest(client):
    reset_supervisor()
    a = client.post(
        "/api/browser/spaces", json={"name": "inbox", "run_id": "run-a"}
    ).json()
    assert a["ok"] is True
    b = client.post(
        "/api/browser/spaces", json={"name": "inbox", "run_id": "run-b"}
    ).json()
    assert b["ok"] is False
    shared = client.post(
        "/api/browser/spaces",
        json={"name": "shared:inbox", "run_id": "run-x"},
    ).json()
    assert shared["ok"] is True
    again = client.post(
        "/api/browser/spaces",
        json={"name": "shared:inbox", "run_id": "run-y", "confirm_shared": True},
    ).json()
    assert again["ok"] is True


def test_browser_node_registered():
    assert "browser" in known_types()
    assert get_node("browser") is not None
    d = {
        "name": "w",
        "entry": "b",
        "nodes": [
            {
                "id": "b",
                "type": "browser",
                "action": "eval",
                "space": "demo",
                "code": "return {ok: true}",
            }
        ],
        "edges": [],
    }
    assert wf_dsl.validate_dsl(wf_dsl.normalize_dsl(d)) == []


def test_browser_complete_cannot_mix_eval_helpers():
    d = {
        "name": "w",
        "entry": "c",
        "nodes": [
            {
                "id": "c",
                "type": "browser",
                "action": "complete",
                "space": "demo",
                "code": "await useOrCreateTaskSpace('demo')\nawait completeTaskSpace({keep:true})",
            }
        ],
        "edges": [],
    }
    errs = wf_dsl.validate_dsl(wf_dsl.normalize_dsl(d))
    assert any("complete" in e for e in errs)


@pytest.mark.asyncio
async def test_browser_node_eval_and_snapshot(isolated_home):
    from ginno_runtime.browser import reset_supervisor
    from ginno_runtime.workflows.nodes.builtin import BrowserNode

    reset_supervisor()
    try:
        node = {
            "id": "b",
            "type": "browser",
            "action": "eval",
            "space": "inbox",
            "url": "ginno://login-wall",
            "code": "cliLog(await snapshotText())\nreturn {ok: true}",
            "writes": {"ok": {"type": "boolean"}},
        }
        cctx = {
            "run_ctx": {
                "run_id": "r1",
                "session_id": "s1",
                "events": [],
            }
        }
        out = await BrowserNode.execute(node, cctx, {"context": {}}, {}, {})
        payload = out["__output__"]
        assert payload.get("ok") is True
        assert payload.get("space") == "inbox"
        snap = await BrowserNode.execute(
            {"id": "s", "type": "browser", "action": "snapshot", "space": "inbox"},
            cctx,
            {"context": {}},
            {},
            {},
        )
        text = snap["__output__"].get("snapshot") or ""
        assert "请先登录" in text
    finally:
        reset_supervisor()


def test_headless_handoff_binds_latest_session(isolated_home):
    from ginno_runtime.api.workflows import _bind_headless_browser_handoff, _latest_session_id
    from ginno_runtime.session_meta import _session_meta_upsert
    from ginno_runtime.workflows import store as wf_storemod

    _session_meta_upsert(
        "default",
        {"id": "sess-latest", "title": "t", "updated": 9_999, "created": 1},
    )
    assert _latest_session_id() == "sess-latest"
    rid = "run-headless"
    wf_storemod._write_json(
        wf_storemod._run_path(rid),
        {
            "id": rid,
            "workflow_id": "w",
            "status": "running",
            "steps": [],
            "present_in_session_id": None,
        },
    )
    sid = _bind_headless_browser_handoff(rid, {"kind": "browser_handoff", "space": "x"})
    assert sid == "sess-latest"
    rebound = wf_storemod.get_run(rid)
    assert rebound["present_in_session_id"] == "sess-latest"


def test_browser_tabs_and_downloads_rest(client):
    reset_supervisor()
    created = client.post("/api/browser/spaces", json={"name": "tabs-me"}).json()
    assert created["ok"] is True
    tabs = client.get("/api/browser/spaces/tabs-me/tabs").json()
    assert tabs["ok"] is True
    assert len(tabs["tabs"]) == 1
    opened = client.post(
        "/api/browser/spaces/tabs-me/tabs", json={"url": "https://example.com/two", "human": True}
    ).json()
    assert opened["ok"] is True
    tabs2 = client.get("/api/browser/spaces/tabs-me/tabs").json()
    assert len(tabs2["tabs"]) == 2
    other = next(t for t in tabs2["tabs"] if not t.get("active"))
    switched = client.post(
        f"/api/browser/spaces/tabs-me/tabs/{other['id']}/activate", json={"human": True}
    ).json()
    assert switched["ok"] is True
    dls = client.get("/api/browser/spaces/tabs-me/downloads").json()
    assert dls["ok"] is True
    assert dls["downloads"] == []
    all_dls = client.get("/api/browser/downloads").json()
    assert all_dls["ok"] is True


def test_risky_navigate_rest_flips_owner(client):
    reset_supervisor()
    client.post("/api/browser/spaces", json={"name": "pay-me"})
    nav = client.post(
        "/api/browser/spaces/pay-me/navigate",
        json={"url": "https://www.alipay.com/pay"},
    ).json()
    assert nav.get("interrupt") == "handoff"
    rec = client.get("/api/browser/spaces/pay-me").json()["space"]
    assert rec["owner"] == "agentDelegatedToUser"
    assert rec["pending_risky_url"]
