"""Unit tests: web_search 诊断检测 + 一键修复端点守卫（2026-11-15）。

check_web_search 是配置级检测（不做网络探针）：默认引擎为 DDG 时报 medium
issue + fix 标记；searxng/禁用态不报。fix 端点的完整安装 turn 由 packaged UI
e2e（test_packaged_ui_web_search_fix.py）覆盖，这里只测纯逻辑与守卫路径。
"""

from __future__ import annotations

import pytest

from ginno_runtime import diagnostics
from ginno_runtime.web import config as wc

pytestmark = pytest.mark.unit


def _patch_cfg(monkeypatch, **over):
    cfg = wc.WebConfig(**over)
    monkeypatch.setattr(wc, "load_web_config", lambda *a, **k: cfg)


def test_check_web_search_flags_ddg_default(monkeypatch):
    _patch_cfg(monkeypatch, enabled=True, default_engine="duckduckgo")
    issues = diagnostics.check_web_search()
    assert len(issues) == 1
    assert issues[0]["id"] == "web.fragile_search_engine"
    assert issues[0]["severity"] == "medium"
    assert issues[0]["fix"] == "web-search-searxng"


def test_check_web_search_quiet_on_searxng(monkeypatch):
    _patch_cfg(monkeypatch, enabled=True, default_engine="searxng")
    assert diagnostics.check_web_search() == []


def test_check_web_search_quiet_when_disabled(monkeypatch):
    _patch_cfg(monkeypatch, enabled=False, default_engine="duckduckgo")
    assert diagnostics.check_web_search() == []


def test_check_web_search_in_report(monkeypatch):
    _patch_cfg(monkeypatch, enabled=True, default_engine="duckduckgo")
    report = diagnostics.run_diagnostics()
    assert "web" in report["sections"]
    assert any(i["fix"] == "web-search-searxng" for i in report["sections"]["web"]["issues"])


def test_fix_endpoint_missing_session_id(client):
    r = client.post("/api/diagnostics/fix/web-search-searxng", json={})
    body = r.json()
    assert body["ok"] is False
    assert "session_id" in body["error"]


def test_fix_endpoint_unknown_session(client):
    r = client.post("/api/diagnostics/fix/web-search-searxng", json={"session_id": "does-not-exist"})
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unknown session"
