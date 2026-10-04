"""Unit tests for the i18n catalog package + locale middleware.

Covers ``_(key, **params)`` (hit / fallback / miss), ``i18n_health_check()``
(key parity + ICU placeholder parity), and ``RequestLocaleMiddleware``
(header parsing on http + websocket scopes, ``?lang=`` query fallback
(browser WS can't send headers), invalid values ignored).
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import i18n, lang, paths, server
from ginno_runtime.server import RequestLocaleMiddleware

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _en_default(isolated_home, monkeypatch):
    """Fresh catalogs + deterministic en default (no OS-locale dependence)."""
    paths.ensure_layout()
    i18n._catalogs.clear()
    monkeypatch.setattr(lang._locale_mod, "getdefaultlocale", lambda: (None, None))
    yield
    i18n._catalogs.clear()


# --------------------------------------------------------------------------- #
# _(key, **params)
# --------------------------------------------------------------------------- #
def test_hit_en():
    assert i18n._("errors.session_not_found", id="abc") == "Session abc not found"


def test_hit_zh_cn():
    token = lang.bind_request_locale("zh-CN")
    try:
        assert i18n._("errors.session_not_found", id="abc") == "会话 abc 不存在"
    finally:
        lang.reset_request_locale(token)


def test_fallback_to_en_when_locale_missing_key():
    # Simulate a zh-CN catalog skew: en must still answer.
    i18n._catalogs["zh-CN"] = {}
    token = lang.bind_request_locale("zh-CN")
    try:
        assert i18n._("errors.session_not_found", id="x") == "Session x not found"
    finally:
        lang.reset_request_locale(token)


def test_missing_key_returns_key_and_warns(caplog):
    with caplog.at_level("WARNING", logger="ginno_runtime.i18n"):
        assert i18n._("no.such_key") == "no.such_key"
    assert any("i18n_missing" in r.message for r in caplog.records)


def test_unknown_params_stay_literal():
    # params/catalog skew must degrade visibly, not raise.
    i18n._catalogs["en"] = {"demo.named": "hi {name}"}
    assert i18n._("demo.named") == "hi {name}"


def test_interpolation_multiple_params():
    i18n._catalogs["en"] = {"demo.multi": "{a} then {b}"}
    assert i18n._("demo.multi", a=1, b=2) == "1 then 2"


# --------------------------------------------------------------------------- #
# i18n_health_check
# --------------------------------------------------------------------------- #
def test_health_check_ok():
    assert i18n.i18n_health_check() == []


def test_health_check_detects_key_gap():
    cats = {
        "en": {"a.b": "A", "c.d": "C"},
        "zh-CN": {"a.b": "甲", "e.f": "乙"},
    }
    orig = i18n._load_catalog
    i18n._load_catalog = lambda locale: dict(cats[locale])  # bypass disk+cache
    try:
        problems = i18n.i18n_health_check()
    finally:
        i18n._load_catalog = orig
    assert any("missing in zh-CN" in p and "c.d" in p for p in problems)
    assert any("missing in en" in p and "e.f" in p for p in problems)


def test_health_check_detects_placeholder_mismatch():
    cats = {"en": {"a.b": "hi {name}"}, "zh-CN": {"a.b": "你好 {user}"}}
    orig = i18n._load_catalog
    i18n._load_catalog = lambda locale: dict(cats[locale])
    try:
        problems = i18n.i18n_health_check()
    finally:
        i18n._load_catalog = orig
    assert len(problems) == 1
    assert "placeholder mismatch for a.b" in problems[0]


# --------------------------------------------------------------------------- #
# RequestLocaleMiddleware
# --------------------------------------------------------------------------- #
def _run_middleware(scope: dict) -> str:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["locale"] = lang.current_locale()

    mw = RequestLocaleMiddleware(app)
    asyncio.run(mw(scope, None, None))
    return seen["locale"]


def test_middleware_http_header_binds_locale():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-ginno-language", b"zh-CN")],
    }
    assert _run_middleware(scope) == "zh-CN"


def test_middleware_ws_header_binds_locale():
    scope = {
        "type": "websocket",
        "path": "/api/ws/sessions/x",
        "headers": [(b"x-ginno-language", b"zh-CN"), (b"host", b"127.0.0.1")],
    }
    assert _run_middleware(scope) == "zh-CN"


def test_middleware_invalid_header_ignored():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-ginno-language", b"fr")],
    }
    assert _run_middleware(scope) == "en"  # settings default, binding skipped


def test_middleware_no_header_falls_back_to_settings():
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    assert _run_middleware(scope) == "en"


# ---- ?lang= query fallback (browser WS can't send custom headers) ---------- #
def test_middleware_ws_query_lang_binds_locale():
    scope = {
        "type": "websocket",
        "path": "/api/ws/sessions/x",
        "query_string": b"lang=zh-CN",
        "headers": [(b"host", b"127.0.0.1")],
    }
    assert _run_middleware(scope) == "zh-CN"


def test_middleware_query_invalid_lang_ignored():
    scope = {
        "type": "websocket",
        "path": "/api/ws/sessions/x",
        "query_string": b"lang=fr",
        "headers": [],
    }
    assert _run_middleware(scope) == "en"  # settings default, binding skipped


def test_middleware_header_wins_over_query():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-ginno-language", b"en")],
        "query_string": b"lang=zh-CN",
    }
    assert _run_middleware(scope) == "en"


def test_middleware_registered_on_app():
    # The middleware must be part of the ASGI stack so every WS endpoint
    # (sessions / relay / runs / schedule) inherits the binding.
    names = [m.cls.__name__ for m in server.app.user_middleware]
    assert "RequestLocaleMiddleware" in names
