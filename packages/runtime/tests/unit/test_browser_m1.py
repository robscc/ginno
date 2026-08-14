"""M1: snapshot flatten, risky URL, shared-space lock, Chrome import status."""

from __future__ import annotations

import json

import pytest

from ginno_runtime.browser.engine import FakeEngine
from ginno_runtime.browser.profile import import_status
from ginno_runtime.browser.risk import DEFAULT_RISKY_DOMAINS, is_risky_url
from ginno_runtime.browser.snapshot import flatten_ax, format_snapshot
from ginno_runtime.browser.ownership import OWNER_DELEGATED
from ginno_runtime.browser.supervisor import BrowserLocked, BrowserSupervisor

pytestmark = pytest.mark.unit


def test_flatten_ax_assigns_refs_to_interactive():
    tree = {
        "nodes": [
            {
                "nodeId": "0",
                "role": {"value": "RootWebArea"},
                "name": {"value": "Login"},
                "childIds": ["1", "2"],
            },
            {
                "nodeId": "1",
                "role": {"value": "textbox"},
                "name": {"value": "密码"},
                "childIds": [],
                "backendDOMNodeId": 11,
            },
            {
                "nodeId": "2",
                "role": {"value": "button"},
                "name": {"value": "登录"},
                "childIds": [],
                "backendDOMNodeId": 12,
            },
        ]
    }
    text, refs = flatten_ax(tree)
    assert "ref=1" in text and "textbox" in text
    assert "ref=2" in text and "button" in text
    assert refs["1"]["name"] == "密码"
    assert refs["2"]["role"] == "button"
    snap = format_snapshot("https://ex", "Login", text)
    assert snap.startswith("[document] Login — https://ex")


def test_is_risky_url_defaults():
    assert is_risky_url("https://www.alipay.com/pay", DEFAULT_RISKY_DOMAINS)
    assert is_risky_url("https://pay.weixin.qq.com/", DEFAULT_RISKY_DOMAINS)
    assert is_risky_url("https://secure.paypal.com/checkout", DEFAULT_RISKY_DOMAINS)
    assert is_risky_url("https://my.bankofexample.com/", DEFAULT_RISKY_DOMAINS)
    assert is_risky_url("https://github.com/settings/security/password", DEFAULT_RISKY_DOMAINS)
    assert not is_risky_url("https://github.com/issues", DEFAULT_RISKY_DOMAINS)
    assert not is_risky_url("ginno://login-wall", DEFAULT_RISKY_DOMAINS)
    assert not is_risky_url("", DEFAULT_RISKY_DOMAINS)


def test_is_risky_url_reads_settings(isolated_home):
    from ginno_runtime import paths

    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"browser": {"risky_domains": ["*://*.example-bank*"]}}))
    assert is_risky_url("https://foo.example-bank.com/x")
    assert not is_risky_url("https://www.alipay.com/pay")


def test_shared_space_lock(isolated_home):
    sup = BrowserSupervisor(engine=FakeEngine())
    try:
        sup.use_or_create("inbox", run_id="run-a")
        with pytest.raises(BrowserLocked, match="bound to run"):
            sup.use_or_create("inbox", run_id="run-b")
        # same run may reuse
        rec = sup.use_or_create("inbox", run_id="run-a")
        assert rec["name"] == "inbox"
        # opt-in shared: prefix + confirm
        first = sup.use_or_create("shared:inbox", run_id="run-x")
        assert first["bound_run_id"] == "run-x"
        with pytest.raises(BrowserLocked, match="bound to run"):
            sup.use_or_create("shared:inbox", run_id="run-y")
        shared = sup.use_or_create("shared:inbox", run_id="run-y", confirm_shared=True)
        assert shared["name"] == "shared:inbox"
    finally:
        sup.close_sync()


def test_risky_navigate_handoffs(isolated_home):
    from ginno_runtime.browser.helpers import BrowserHandoff

    sup = BrowserSupervisor(engine=FakeEngine())
    try:
        rec = sup.use_or_create("pay")
        with pytest.raises(BrowserHandoff) as ei:
            sup.navigate(rec["name"], "https://www.alipay.com/pay")
        assert "high-risk" in ei.value.reason
        rec = sup.get_space("pay")
        assert rec["pending_risky_url"]
        assert rec["owner"] == OWNER_DELEGATED
        assert sup.waiting_human() is True
    finally:
        sup.close_sync()


def test_import_status_without_chrome(isolated_home, monkeypatch):
    monkeypatch.delenv("GINNO_CHROME_USER_DATA", raising=False)
    from ginno_runtime.browser import profile as prof

    monkeypatch.setattr(prof, "default_chrome_user_data", lambda: None)
    monkeypatch.setattr(prof, "chrome_is_running", lambda: False)
    st = import_status()
    assert st["imported"] is False
    assert st["profiles"] == []
    assert st["chrome_user_data"] == ""


def test_screenshot_persists_png(isolated_home):
    sup = BrowserSupervisor(engine=FakeEngine())
    try:
        rec = sup.use_or_create("shot")
        out = sup.screenshot(rec["name"])
        assert out["ok"] is True
        from pathlib import Path

        p = Path(out["path"])
        assert p.is_file()
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        sup.close_sync()


def test_state_reports_fake_engine(isolated_home):
    sup = BrowserSupervisor(engine=FakeEngine())
    try:
        st = sup.state()
        assert st["engine"] == "fake"
        assert st["headed"] is False
        assert st.get("engine_error") in (None, "")
    finally:
        sup.close_sync()


def test_reap_drops_stale_singleton(isolated_home, tmp_path):
    from ginno_runtime.browser.engine import _reap_stale_chrome

    lock = tmp_path / "SingletonLock"
    lock.write_text("stale")
    sock = tmp_path / "SingletonSocket"
    sock.write_text("x")
    _reap_stale_chrome(tmp_path)
    assert not lock.exists()
    assert not sock.exists()


def test_latest_frame_and_human_input(isolated_home):
    sup = BrowserSupervisor(engine=FakeEngine())
    try:
        rec = sup.use_or_create("wall")
        sup.navigate(rec["name"], "ginno://login-wall")
        frame = sup.latest_frame(rec["name"])
        assert frame["bytes"].startswith(b"<svg")
        assert frame["mime"] == "image/svg+xml"
        # Hover must not steal the Space.
        with pytest.raises(BrowserLocked, match="agent-owned"):
            sup.dispatch_input(rec["name"], {"type": "mouseMoved", "x": 10, "y": 10})
        # A real click claims the Space — no extra 「接管」 click.
        clicked = sup.dispatch_input(rec["name"], {"type": "mousePressed", "x": 10, "y": 10})
        assert clicked.get("ok") is True
        assert clicked.get("handoff") is True
        assert sup.get_space(rec["name"])["owner"] == OWNER_DELEGATED
        typed = sup.dispatch_input(rec["name"], {"type": "keyDown", "key": "s", "text": "s"})
        assert typed.get("ok") is True
        vp = sup.set_viewport(640, 480)
        assert vp["ok"] is True
        assert vp["width"] == 640
    finally:
        sup.close_sync()


def test_chrome_new_tab_uses_put(isolated_home, monkeypatch):
    """Regression: GET /json/new → HTTP 405, Space stuck at about:blank."""
    from ginno_runtime.browser.engine import ChromeEngine

    calls: list[tuple[str, str | None]] = []

    eng = ChromeEngine.__new__(ChromeEngine)
    eng._tabs = {}
    eng._sessions = {}

    def _page_targets(_self):
        return [{"id": "existing", "type": "page", "url": "https://x.com/"}]

    def _http(_self, path, payload=None, *, method=None):
        calls.append((path, method))
        if path.startswith("/json/new"):
            if method != "PUT":
                raise RuntimeError("HTTP Error 405: Method Not Allowed")
            return {"id": "new-tab", "type": "page", "url": "about:blank"}
        return []

    monkeypatch.setattr(ChromeEngine, "_page_targets", _page_targets)
    monkeypatch.setattr(ChromeEngine, "_http", _http)
    monkeypatch.setattr(ChromeEngine, "_drop_session", lambda _self, tid: None)
    ChromeEngine.ensure_space(eng, "sf-ai-site")
    assert eng._tabs["sf-ai-site"] == "new-tab"
    assert any(p.startswith("/json/new") and m == "PUT" for p, m in calls)
    assert eng._tabs["sf-ai-site"] != "existing"


def test_fake_tabs_are_space_scoped(isolated_home):
    eng = FakeEngine()
    eng.ensure_space("a")
    eng.ensure_space("b")
    eng.navigate("a", "https://a.example/")
    extra = eng.open_tab("a", "https://a.example/two")
    assert extra["id"]
    tabs_a = eng.list_tabs("a")
    tabs_b = eng.list_tabs("b")
    assert len(tabs_a) == 2
    assert len(tabs_b) == 1
    assert all(t["id"].startswith("a:") for t in tabs_a)
    assert all(t["id"].startswith("b:") for t in tabs_b)
    closed = eng.close_tab("a", extra["id"])
    assert closed["ok"] is True
    assert len(eng.list_tabs("a")) == 1
    last = eng.close_tab("b")
    assert last["ok"] is False


def test_merge_ax_frames_continues_refs():
    from ginno_runtime.browser.snapshot import merge_ax_frames

    main = {
        "nodes": [
            {
                "nodeId": "0",
                "role": {"value": "RootWebArea"},
                "name": {"value": "Main"},
                "childIds": ["1"],
            },
            {
                "nodeId": "1",
                "role": {"value": "button"},
                "name": {"value": "外层"},
                "childIds": [],
                "backendDOMNodeId": 11,
            },
        ]
    }
    iframe = {
        "nodes": [
            {
                "nodeId": "0",
                "role": {"value": "RootWebArea"},
                "name": {"value": "Inner"},
                "childIds": ["1"],
            },
            {
                "nodeId": "1",
                "role": {"value": "textbox"},
                "name": {"value": "框"},
                "childIds": [],
                "backendDOMNodeId": 21,
            },
        ]
    }
    text, refs = merge_ax_frames([("", main), ("child", iframe), ("ads", None)])
    assert refs["1"]["name"] == "外层"
    assert refs["2"]["name"] == "框"
    assert "[iframe] child" in text
    assert "cross-origin, omitted" in text


def test_download_record_and_list(isolated_home):
    from ginno_runtime.browser import downloads as dl

    rec = dl.record(
        {
            "id": "g1",
            "space": "inbox",
            "filename": "report.csv",
            "path": str(isolated_home / "report.csv"),
            "state": "completed",
            "url": "https://ex/report.csv",
        }
    )
    assert rec["filename"] == "report.csv"
    listed = dl.list_downloads("inbox")
    assert len(listed) == 1
    assert listed[0]["id"] == "g1"
    assert dl.list_downloads("other") == []


def test_cef_framework_without_helpers_is_not_live(isolated_home, tmp_path, monkeypatch):
    from ginno_runtime.browser import cef as cefmod

    fw = tmp_path / "Frameworks" / "Chromium Embedded Framework.framework"
    fw.mkdir(parents=True)
    monkeypatch.setenv("GINNO_CEF_DIR", str(tmp_path / "Frameworks"))
    assert cefmod.cef_runtime_dir() == tmp_path / "Frameworks"
    assert cefmod.cef_helpers_present() is False
    assert cefmod.try_cef() is None
    helper = tmp_path / "Frameworks" / "Ginno Helper.app"
    helper.mkdir()
    assert cefmod.cef_helpers_present() is True
    # Helpers exist but the host never wrote a live CDP port — do not construct.
    assert cefmod.try_cef() is None
    # A leftover status file without a listening CDP is still not live.
    from ginno_runtime.browser import spaces as space_store

    space_store.ensure_browser_layout()
    cefmod.cef_status_path().write_text(
        '{"ready": true, "port": 1, "pid": 0, "error": ""}'
    )
    assert cefmod.try_cef() is None


def test_choose_engine_cef_falls_back_in_tests(isolated_home, monkeypatch):
    from ginno_runtime.browser import engine as engmod
    from ginno_runtime.browser.cef import try_cef

    assert try_cef() is None
    monkeypatch.setenv("GINNO_BROWSER_ENGINE", "cef")
    # Drop the pytest sentinel so we actually take the CEF branch.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(engmod, "_find_chrome", lambda: None)
    picked = engmod.choose_engine()
    assert isinstance(picked, FakeEngine)
    err = engmod.last_engine_error() or ""
    assert "CEF" in err or "Chrome" in err or "Chromium" in err


def test_try_cef_live_when_host_cdp_answers(isolated_home, tmp_path, monkeypatch):
    """Helpers + a real CDP listener + status file → CefEngine, not a fake."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from ginno_runtime.browser import cef as cefmod
    from ginno_runtime.browser import spaces as space_store

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"Browser":"CEF","Protocol-Version":"1.3"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        fw = tmp_path / "Frameworks" / "Chromium Embedded Framework.framework"
        fw.mkdir(parents=True)
        (tmp_path / "Frameworks" / "Ginno Helper.app").mkdir()
        monkeypatch.setenv("GINNO_CEF_DIR", str(tmp_path / "Frameworks"))
        space_store.ensure_browser_layout()
        cefmod.cef_status_path().write_text(
            json.dumps({"ready": True, "port": port, "pid": 0, "error": ""})
        )
        eng = cefmod.try_cef()
        assert eng is not None
        assert getattr(eng, "kind", None) == "cef"
        assert eng.headed() is True
        eng.close()
    finally:
        srv.shutdown()


def test_supervisor_tabs_and_state_engine_kind(isolated_home):
    sup = BrowserSupervisor(engine=FakeEngine())
    try:
        rec = sup.use_or_create("tabs")
        opened = sup.open_tab(rec["name"], "https://example.com/n")
        assert opened["ok"] is True
        tabs = sup.list_tabs(rec["name"])
        assert len(tabs) == 2
        st = sup.state()
        assert st["engine"] == "fake"
    finally:
        sup.close_sync()
