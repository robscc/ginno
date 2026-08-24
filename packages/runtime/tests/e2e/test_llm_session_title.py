"""WebSocket e2e: the LLM subject title replaces the truncated placeholder.

The first user message still becomes a 40-char truncated title at turn start
(instant sidebar label); once the first turn completes, a background
auxiliary call summarizes the first exchange into a subject title and pushes
a second ``session_title`` event.
"""

from __future__ import annotations

import json
import time

import pytest
from conftest import event_names, events_of

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.e2e


def _meta(sid: str) -> dict:
    index = json.loads(paths.session_index_path("default").read_text())
    return next(m for m in index if m["id"] == sid)


def _wait_title(sid: str, placeholder: str, timeout: float = 5.0) -> dict:
    """Poll the on-disk meta until the bg title task rewrites it, so a
    regression fails fast instead of blocking the socket recv forever."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = _meta(sid)
        if meta["title"] != placeholder:
            return meta
        time.sleep(0.05)
    return _meta(sid)


def test_llm_title_replaces_truncated_after_first_turn(create_session, ws_conv, monkeypatch):
    sid = create_session([script(text="Photosynthesis converts light into chemical energy.")])
    # the auxiliary title call builds its own model (never the session's
    # scripted one) — point title_gen.build_model at the title script.
    from ginno_runtime import title_gen

    monkeypatch.setattr(
        title_gen,
        "build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text="Photosynthesis basics")]),
    )
    user_text = "what is photosynthesis? explain briefly"
    placeholder = user_text.replace("\n", " ")[:40]
    with ws_conv(sid) as conv:
        conv.invoke(user_text)
        events = conv.recv_until("message.end", "error")
        assert "error" not in event_names(events)
        # placeholder title event fired at turn start (truncated first message)
        first = events_of(events, "session_title")[0]
        assert first["title"] == placeholder
        # the background LLM title rewrites the meta, then pushes the event
        meta = _wait_title(sid, placeholder)
        ev = conv.recv_until("session_title")[-1]
    assert ev["title"] == "Photosynthesis basics"
    assert meta["title"] == "Photosynthesis basics"
    assert meta["title_llm_pending"] is False
    # seed/assistant are cleared once the LLM title applied
    assert meta["title_seed"] == ""
    assert meta["title_assistant"] == ""


def test_llm_title_failure_keeps_truncated(create_session, ws_conv):
    # No patched title model here: title_gen.build_model raises for the
    # disabled test provider -> the auxiliary call skips, the truncated
    # placeholder sticks and the pending flag stays armed for a retry on
    # the next completed turn.
    sid = create_session([script(text="Hello.")])
    with ws_conv(sid) as conv:
        conv.invoke("hi there")
        events = conv.recv_until("message.end", "error")
        assert "error" not in event_names(events)
    # give the background task a beat to run and prove it does not overwrite
    time.sleep(0.3)
    meta = _meta(sid)
    assert meta["title"] == "hi there"
    assert meta["title_llm_pending"] is True
