"""Unit tests for locale resolution + language migration (lang.py / paths.py).

``language`` (``"auto"|"en"|"zh-CN"``) replaces the legacy ``prompt_language``
(i18n-design.md §2). Priority: request header binding (contextvar, bound by
the ASGI middleware in server.py) > settings; the ``GINNO_LANGUAGE`` env
overrides everything (tests/dev).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ginno_runtime import lang, paths

pytestmark = pytest.mark.unit


def _write_settings(**kv) -> None:
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kv))


@pytest.fixture
def no_os_locale(monkeypatch):
    """Pin the ``auto`` resolution away from the dev machine's real locale."""
    monkeypatch.setattr(lang._locale_mod, "getdefaultlocale", lambda: (None, None))


# --------------------------------------------------------------------------- #
# settings_locale (settings file / env / auto)
# --------------------------------------------------------------------------- #
def test_default_is_en(no_os_locale, isolated_home):
    assert lang.settings_locale() == "en"
    assert lang.t("hello", "你好") == "hello"


def test_settings_zh_cn(isolated_home):
    _write_settings(language="zh-CN")
    assert lang.t("hello", "你好") == "你好"


def test_settings_auto_zh_os(isolated_home, monkeypatch):
    _write_settings(language="auto")
    monkeypatch.setattr(
        lang._locale_mod, "getdefaultlocale", lambda: ("zh_CN", "UTF-8")
    )
    assert lang.settings_locale() == "zh-CN"


def test_settings_auto_unsupported_os_lang_lands_en(isolated_home, monkeypatch):
    # ja/fr/… silently resolve to en (i18n-design.md §2)
    _write_settings(language="auto")
    monkeypatch.setattr(
        lang._locale_mod, "getdefaultlocale", lambda: ("ja_JP", "UTF-8")
    )
    assert lang.settings_locale() == "en"


def test_invalid_language_value_falls_to_auto(no_os_locale, isolated_home):
    _write_settings(language="fr")
    assert lang.settings_locale() == "en"


def test_env_override_wins(isolated_home, monkeypatch):
    _write_settings(language="zh-CN")
    monkeypatch.setenv("GINNO_LANGUAGE", "en")
    assert lang.settings_locale() == "en"
    monkeypatch.setenv("GINNO_LANGUAGE", "ZH")  # dev convenience spelling
    assert lang.settings_locale() == "zh-CN"


def test_unreadable_settings_falls_back_to_en(no_os_locale, isolated_home):
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert lang.settings_locale() == "en"


# --------------------------------------------------------------------------- #
# request binding (contextvar) outranks settings
# --------------------------------------------------------------------------- #
def test_request_binding_outranks_settings(isolated_home):
    _write_settings(language="en")
    token = lang.bind_request_locale("zh-CN")
    try:
        assert lang.current_locale() == "zh-CN"
        assert lang.t("hello", "你好") == "你好"
    finally:
        lang.reset_request_locale(token)
    assert lang.current_locale() == "en"


def test_invalid_binding_ignored(isolated_home):
    _write_settings(language="en")
    assert lang.bind_request_locale("fr") is None
    assert lang.current_locale() == "en"


def test_binding_inherited_by_async_task(isolated_home):
    """asyncio.create_task copies the calling context — turn jobs spawned
    inside a WS connection keep the header-bound locale for the whole turn."""

    _write_settings(language="en")

    async def _read() -> str:
        return lang.current_locale()

    async def _conn() -> str:
        lang.bind_request_locale("zh-CN")
        return await asyncio.create_task(_read())

    assert asyncio.run(_conn()) == "zh-CN"


# --------------------------------------------------------------------------- #
# response language directive (i18n-design.md §6)
# --------------------------------------------------------------------------- #
def test_response_lang_directive_follows_locale(isolated_home):
    _write_settings(language="en")
    assert "English" in lang.response_lang_directive()
    token = lang.bind_request_locale("zh-CN")
    try:
        assert "Simplified Chinese" in lang.response_lang_directive()
    finally:
        lang.reset_request_locale(token)


# --------------------------------------------------------------------------- #
# settings migration (i18n-design.md §2)
# --------------------------------------------------------------------------- #
def test_migrate_legacy_zh(isolated_home):
    s = {"prompt_language": "zh"}
    assert paths._migrate_language(s) is True
    assert s == {"language": "zh-CN"}


def test_migrate_legacy_en(isolated_home):
    s = {"prompt_language": "en"}
    assert paths._migrate_language(s) is True
    assert s == {"language": "en"}


def test_migrate_fresh_install_auto(isolated_home):
    s = {}
    assert paths._migrate_language(s) is True
    assert s == {"language": "auto"}


def test_migrate_existing_language_wins(isolated_home):
    s = {"language": "en", "prompt_language": "zh"}
    assert paths._migrate_language(s) is True
    assert s == {"language": "en"}  # prompt_language still popped


def test_migrate_idempotent_when_already_new(isolated_home):
    s = {"language": "zh-CN"}
    assert paths._migrate_language(s) is False
    assert s == {"language": "zh-CN"}


def test_ensure_layout_writes_migration_back(isolated_home):
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"prompt_language": "zh"}))
    paths.ensure_layout()
    data = json.loads(p.read_text())
    assert data["language"] == "zh-CN"
    assert "prompt_language" not in data
