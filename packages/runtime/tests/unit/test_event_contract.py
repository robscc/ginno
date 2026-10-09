"""Event-contract tests (i18n-design.md §3, P2 一次性全切).

Every runtime → web user-visible event payload carries the triple
``text`` (English fallback, pre-existing field) + ``i18n_key`` (dot path in
the web catalog) + optional ``params`` (ICU-style ``{name}``). These tests
pin the contract from both ends:

* every ``i18n_key`` literal emitted by the stream layer exists in the web
  catalogs (en AND zh-CN, key parity);
* the payload shape carries all three fields;
* the history context rows (summary / reinject) carry key + params;
* the runtime-side catalog (`_()`) renders the scheduler copy in both
  locales;
* the system prompt carries the response-language directive per locale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ginno_runtime import i18n, lang
from ginno_runtime.api.stream.ws import _ev
from ginno_runtime.server_shared import _ev as _ev_shared

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MESSAGES_DIR = _REPO_ROOT / "apps" / "web" / "messages"

# (file, keys the stream layer must emit literals for)
_STREAM_SOURCES = (
    Path("packages/runtime/src/ginno_runtime/api/stream/ws.py"),
    Path("packages/runtime/src/ginno_runtime/api/stream/engine.py"),
)

_KEY_RE = re.compile(r'"i18n_key":\s*"([a-z0-9_.]+)"')


def _load_catalog(locale_dir: str, domain: str) -> dict:
    path = _MESSAGES_DIR / locale_dir / f"{domain}.json"
    assert path.exists(), f"missing catalog file: {path}"
    return json.loads(path.read_text("utf-8"))


# --------------------------------------------------------------------------- #
# key parity: runtime emissions ↔ web catalogs
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _MESSAGES_DIR.exists(), reason="web checkout not present")
def test_stream_i18n_keys_exist_in_web_catalogs():
    referenced: set[str] = set()
    base = _REPO_ROOT
    for rel in _STREAM_SOURCES:
        src = (base / rel).read_text("utf-8")
        referenced |= set(_KEY_RE.findall(src))
    assert referenced, "stream layer must reference at least one i18n_key"
    for locale_dir in ("en", "zh-CN"):
        cat = _load_catalog(locale_dir, "stream")["stream"]
        missing = sorted(k for k in referenced if k.removeprefix("stream.") not in cat)
        assert not missing, f"{locale_dir} catalog missing keys: {missing}"


@pytest.mark.skipif(not _MESSAGES_DIR.exists(), reason="web checkout not present")
def test_web_stream_catalog_key_parity_en_zh():
    def _flatten(cat: dict, prefix: str = "") -> dict[str, str]:
        flat: dict[str, str] = {}
        for k, v in cat.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                flat.update(_flatten(v, key))
            else:
                flat[key] = v
        return flat

    en = _flatten(_load_catalog("en", "stream")["stream"])
    zh = _flatten(_load_catalog("zh-CN", "stream")["stream"])
    assert set(en) == set(zh)
    # ICU placeholder parity ({name} same names both sides)
    ph = re.compile(r"\{([a-z_]+)\}")
    for key, entext in en.items():
        assert sorted(ph.findall(entext)) == sorted(ph.findall(zh[key])), key


@pytest.mark.skipif(not _MESSAGES_DIR.exists(), reason="web checkout not present")
def test_turn_failed_note_keys_exist_in_web_catalogs():
    """turn_error_fields builds its key dynamically (suffix per failure
    class), so the literal scan in test_stream_i18n_keys_exist_in_web_catalogs
    can't see it — pin the suffix set against the catalogs explicitly."""
    for locale_dir in ("en", "zh-CN"):
        cat = _load_catalog(locale_dir, "stream")["stream"]["turn_failed_note"]
        assert set(cat) == {
            "recursion_limit",
            "empty_input",
            "state",
            "node_timeout",
            "content_filter",
        }


@pytest.mark.skipif(not _MESSAGES_DIR.exists(), reason="web checkout not present")
def test_summary_keys_exist_in_web_catalogs():
    from ginno_runtime.world_state import reinject_row_parts, summary_row_parts

    _, sum_key, _ = summary_row_parts("[conversation summary]\nlead\n\nbody")
    _, rein_key, _ = reinject_row_parts("[world state re-injection]\nx")
    for locale_dir in ("en", "zh-CN"):
        cat = _load_catalog(locale_dir, "summary")["summary"]
        assert sum_key.removeprefix("summary.") in cat
        assert rein_key.removeprefix("summary.") in cat


# --------------------------------------------------------------------------- #
# payload shape: text + i18n_key (+ params) triple
# --------------------------------------------------------------------------- #
def test_ev_payload_carries_contract_triple():
    raw = _ev(
        "error",
        {
            "message": "unknown session: abc",
            "i18n_key": "stream.error_unknown_session",
            "params": {"id": "abc"},
        },
    )
    payload = json.loads(raw)
    assert payload["message"] == "unknown session: abc"  # English fallback text
    assert payload["i18n_key"] == "stream.error_unknown_session"
    assert payload["params"] == {"id": "abc"}


def test_ev_turn_id_survives_alongside_contract_fields():
    raw = _ev_shared(
        "notice",
        {"message": "busy", "i18n_key": "stream.busy_wait"},
        "turn-1",
    )
    payload = json.loads(raw)
    assert payload["turn_id"] == "turn-1"
    assert payload["i18n_key"] == "stream.busy_wait"


def test_params_optional_when_no_placeholders():
    payload = json.loads(
        _ev("error", {"message": "invalid JSON", "i18n_key": "stream.error_invalid_json"})
    )
    assert "params" not in payload  # optional field: omitted when unneeded
    assert payload["i18n_key"] == "stream.error_invalid_json"


def test_summary_row_parts_triple():
    from ginno_runtime.world_state import (
        SUMMARY_LEAD_IN_EN,
        summary_row_parts,
    )

    text, key, params = summary_row_parts(
        f"[conversation summary]\n{SUMMARY_LEAD_IN_EN}\n\nfirst line"
    )
    assert text == "🗂 Earlier conversation compacted into a summary: first line"
    assert key == "summary.compacted_row"
    assert params == {"preview": ": first line"}


def test_reinject_row_parts_triple():
    from ginno_runtime.world_state import reinject_row_parts

    text, key, params = reinject_row_parts("[world state re-injection]\nbody")
    assert text == "🌍 Current world state re-injected"
    assert key == "summary.reinjected_row"
    assert params == {}


# --------------------------------------------------------------------------- #
# runtime-side catalog: scheduler copy (background jobs → settings locale)
# --------------------------------------------------------------------------- #
def test_schedule_catalog_renders_en(isolated_home):
    token = lang.bind_request_locale("en")
    try:
        assert i18n._("schedule.run_succeeded") == "Succeeded"
        assert i18n._("schedule.run_failed", error="boom") == "Failed: boom"
        assert i18n._("schedule.run_interrupted") == "Run interrupted (app quit)"
        assert i18n._("schedule.missed") == "Missed (machine asleep or app not running)"
        assert i18n._("schedule.default_task_title") == "Scheduled task"
    finally:
        lang.reset_request_locale(token)


def test_schedule_catalog_renders_zh(isolated_home):
    token = lang.bind_request_locale("zh-CN")
    try:
        assert i18n._("schedule.run_succeeded") == "已成功"
        assert i18n._("schedule.run_failed", error="boom") == "失败：boom"
        assert i18n._("schedule.default_task_title") == "定时任务"
    finally:
        lang.reset_request_locale(token)


def test_runtime_catalog_health_after_schedule_keys():
    # key + ICU placeholder parity between the runtime's own catalogs
    assert i18n.i18n_health_check() == []


# --------------------------------------------------------------------------- #
# response-language directive in the stable system prompt
# --------------------------------------------------------------------------- #
def _agent_stub():
    from ginno_runtime.agents.registry import AgentConfig

    return AgentConfig(id="t", name="T", system_prompt="You are T.", tools_allow=["*"])


def test_stable_system_carries_response_directive_en(isolated_home):
    from ginno_runtime.graph import build_stable_system

    token = lang.bind_request_locale("en")
    try:
        text = build_stable_system(_agent_stub(), "default", [], agent_id="t")
        assert "Always respond in English." in text
    finally:
        lang.reset_request_locale(token)


def test_stable_system_carries_response_directive_zh(isolated_home):
    from ginno_runtime.graph import build_stable_system

    token = lang.bind_request_locale("zh-CN")
    try:
        text = build_stable_system(_agent_stub(), "default", [], agent_id="t")
        assert "Simplified Chinese" in text
        assert "Always respond in English." not in text
    finally:
        lang.reset_request_locale(token)


def test_stable_system_byte_identical_within_locale(isolated_home):
    # the directive must not break the stable-layer prefix-cache invariant
    from ginno_runtime.graph import build_stable_system

    token = lang.bind_request_locale("en")
    try:
        a = build_stable_system(_agent_stub(), "default", [], agent_id="t")
        b = build_stable_system(_agent_stub(), "default", [], agent_id="t")
        assert a == b
    finally:
        lang.reset_request_locale(token)


# --------------------------------------------------------------------------- #
# attachment-only default intent follows the request locale
# --------------------------------------------------------------------------- #
def test_attach_only_text_bilingual(isolated_home):
    from ginno_runtime.api.stream.turn import _attach_only_text

    token = lang.bind_request_locale("en")
    try:
        assert _attach_only_text().startswith("Summarize the files")
    finally:
        lang.reset_request_locale(token)
    token = lang.bind_request_locale("zh-CN")
    try:
        assert _attach_only_text().startswith("请概览我附加的文件")
    finally:
        lang.reset_request_locale(token)
