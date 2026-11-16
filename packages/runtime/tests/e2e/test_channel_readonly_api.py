"""E2E: channel sessions (wecom/dingtalk) are read-only at the API layer too.

2026-11-16: the desktop renders channel sessions read-only (ChatStream
isChannel), but a raw WS invoke/steer could still converse into them — a
UI-only restriction is no restriction. The WS gate must reject invoke, steer,
and @session delivery targeting a channel session, while the adapters' own
in-process path stays unaffected. Legacy sessions carrying only the channel
field (pre type-marker) are covered too.
"""

from __future__ import annotations

import pytest
from conftest import as_fake_model
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def _mk_session(client, patch_build_model, **extra):
    patch_build_model(as_fake_model(script(text="ok")))
    r = client.post(
        "/api/sessions",
        json={"project_slug": "default", "workspace": "/tmp", **extra},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok"), body
    return body.get("session_id") or body.get("id")


def _is_readonly_error(events):
    return any(
        e.get("event") == "error"
        and e.get("i18n_key") == "stream.error_channel_readonly"
        for e in events
    )


def test_invoke_into_wecom_session_rejected(client, ws_conv, patch_build_model):
    sid = _mk_session(client, patch_build_model, type="channel", channel="wecom", title="ro-wecom")
    with ws_conv(sid) as conv:
        conv.invoke("hello from desktop")
        events = conv.recv_until("error", "message.end")
    assert _is_readonly_error(events), events
    assert not any(e.get("event") == "turn.start" for e in events), (
        "no turn may start in a channel session via WS invoke"
    )


def test_invoke_into_dingtalk_session_rejected(client, ws_conv, patch_build_model):
    sid = _mk_session(client, patch_build_model, type="channel", channel="dingtalk", title="ro-ding")
    with ws_conv(sid) as conv:
        conv.invoke("hello from desktop")
        events = conv.recv_until("error", "message.end")
    assert _is_readonly_error(events), events


def test_steer_into_channel_session_rejected(client, ws_conv, patch_build_model):
    sid = _mk_session(client, patch_build_model, type="channel", channel="wecom", title="ro-steer")
    with ws_conv(sid) as conv:
        conv.send({"type": "steer", "text": "inject", "steer_id": "s-ro"})
        events = conv.recv_until("error", "message.end")
    assert _is_readonly_error(events), events


def test_legacy_channel_field_only_rejected(client, ws_conv, patch_build_model):
    # Sessions created before the type marker carry only `channel`.
    sid = _mk_session(client, patch_build_model, channel="wecom", title="ro-legacy")
    with ws_conv(sid) as conv:
        conv.invoke("hello from desktop")
        events = conv.recv_until("error", "message.end")
    assert _is_readonly_error(events), events


def test_normal_session_invoke_unaffected(client, ws_conv, patch_build_model):
    sid = _mk_session(client, patch_build_model, title="ro-normal")
    patch_build_model(as_fake_model(script(text="normal reply")))
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        events = conv.recv_until("message.end", "error")
    assert any(e.get("event") == "message.end" for e in events), events
    assert not _is_readonly_error(events), events