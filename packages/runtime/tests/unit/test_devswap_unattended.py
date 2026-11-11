"""G1 无头 finish：mark_unattended 状态机（design §3.2）。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ginno_runtime.api import devswap

EVENTS: list[tuple[str, dict]] = []


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    home = tmp_path / "ginno-home"
    home.mkdir()
    (home / "dev-override").mkdir()
    p = home / "dev-override" / "state.json"
    monkeypatch.setenv("GINNO_HOME", str(home))
    monkeypatch.setattr(devswap, "_state_path", lambda: p)
    EVENTS.clear()

    async def fake_push(event: str, data: dict) -> None:
        EVENTS.append((event, data))

    monkeypatch.setattr(devswap.server_shared, "_push_global_event", fake_push)
    return p


def _seed(p: Path, **kw) -> None:
    base = {"state": "pending", "unattended": "confirm", "chat_key": "ck1",
            "created": time.time(), "new": "run-1", "session_id": "s1"}
    base.update(kw)
    p.write_text(json.dumps(base))


def _mark(ck: str, verdict: str) -> dict:
    async def run() -> dict:
        r = devswap.mark_unattended(ck, verdict)
        await asyncio.sleep(0)  # 让 create_task 的广播落地
        return r

    return asyncio.run(run())


def test_confirm_go_writes_and_bcast(_state_file):
    _seed(_state_file)
    r = _mark("ck1", "go")
    assert r["ok"] is True
    st = json.loads(_state_file.read_text())
    assert st["unattended"] == "go" and st["state"] == "pending"
    assert EVENTS and EVENTS[0][0] == "runtime.dev_swap_pending"
    assert EVENTS[0][1].get("auto") is True


def test_abort_keeps_pending_no_bcast(_state_file):
    _seed(_state_file)
    r = _mark("ck1", "abort")
    assert r["ok"] is True
    st = json.loads(_state_file.read_text())
    assert st["unattended"] == "abort" and st["state"] == "pending"
    assert EVENTS == []


def test_chat_key_mismatch_rejected(_state_file):
    _seed(_state_file)
    assert _mark("ck-other", "go")["error"] == "chat_key_mismatch"


def test_stale_ts_rejected(_state_file):
    _seed(_state_file, created=time.time() - 9999)
    assert _mark("ck1", "go")["error"] == "expired"


def test_not_confirmable_when_plain_desktop_run(_state_file):
    _seed(_state_file, unattended="")  # 手动 dev-swap：无 unattended
    assert _mark("ck1", "go")["error"] == "not_confirmable"


def test_bad_verdict(_state_file):
    _seed(_state_file)
    assert devswap.mark_unattended("ck1", "yes")["ok"] is False


def test_verified_state_not_confirmable(_state_file):
    _seed(_state_file, state="verified")
    assert _mark("ck1", "go")["error"] == "not_confirmable"
