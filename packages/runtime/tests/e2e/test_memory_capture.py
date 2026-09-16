"""E2E: memory capture after a turn + refinery gates (capture flag, auto-draft)."""

from __future__ import annotations

import json
import time

import pytest

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def _set_knowledge(**overrides) -> None:
    sp = paths.settings_path()
    settings = json.loads(sp.read_text()) if sp.exists() else {}
    settings["knowledge"] = {**(settings.get("knowledge") or {}), **overrides}
    sp.write_text(json.dumps(settings))


def test_capture_after_turn(client, create_session, ws_conv):
    """After a turn with text output, the pool should have the captured text."""
    sid = create_session([script(text="the assistant reply text")])
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")

    # pool should have 1 entry (the captured assistant text)
    mem = client.get("/api/memory").json()
    assert mem["ok"] is True
    assert mem["pool_count"] >= 1


def test_capture_disabled_skips_pool(client, create_session, ws_conv):
    """`capture: false` (revived dead config) stops pool capture entirely."""
    _set_knowledge(capture=False)
    sid = create_session([script(text="should not be captured")])
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")

    mem = client.get("/api/memory").json()
    assert mem["pool_count"] == 0


def test_auto_distill_at_threshold_produces_draft(client, create_session, ws_conv, monkeypatch):
    """A5b: pool reaching pool_flush_threshold auto-distills a DRAFT in the
    background — never a silent MEMORY.md overwrite. The draft awaits review."""
    _set_knowledge(auto_summarize=True, pool_flush_threshold=1)

    from ginno_runtime.testing.fake_model import ScriptedChatModel

    fake = ScriptedChatModel(scripts=[script(text="## 草稿\n- 自动起草的内容")])
    monkeypatch.setattr(
        "ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake
    )

    sid = create_session([script(text="turn one content")])
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        events = conv.recv_until("message.end", "error")

    # the turn completed with a capture
    assert client.get("/api/memory").json()["pool_count"] >= 1

    # the background auto-distill lands the draft (poll — spawn_bg task)
    deadline = time.time() + 10
    while time.time() < deadline:
        if paths.memory_draft_path().exists():
            break
        time.sleep(0.1)
    assert paths.memory_draft_path().exists(), "auto-draft never appeared"

    # gate held: MEMORY.md untouched, pool kept until review
    mem = client.get("/api/memory").json()
    assert mem["draft_pending"] is True
    assert "自动起草的内容" not in mem["content"]
    assert mem["pool_count"] >= 1
    # the pending draft carries the auto trigger for the review UI
    d = client.get("/api/memory/draft").json()
    assert d["trigger"] == "auto"
    assert "自动起草的内容" in (d["draft"] or "")
