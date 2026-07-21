"""E2E: privileged (bypass_permissions) mode lets every tool through, no prompt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import event_names
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def _set_bypass(isolated_home: Path, value: bool) -> None:
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = value
    sp.write_text(json.dumps(s))


def test_bypass_on_executes_write_without_prompt(client, create_session, ws_conv, isolated_home):
    _set_bypass(isolated_home, True)  # override the test-default (False)
    ws = str(isolated_home / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hi", "workspace": ws})]),
            script(text="wrote it"),
        ],
        workspace=ws,
    )
    with ws_conv(sid) as conv:
        conv.invoke("write a file")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert "permission.request" not in names  # privileged → no prompt
    assert "tool.end" in names
    assert (Path(ws) / "out.txt").read_text(encoding="utf-8") == "hi"  # actually ran


def test_bypass_off_asks_for_write(client, create_session, ws_conv, isolated_home):
    _set_bypass(isolated_home, False)
    ws = str(isolated_home / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hi", "workspace": ws})]),
            script(text="wrote it"),
        ],
        workspace=ws,
    )
    with ws_conv(sid) as conv:
        conv.invoke("write a file")
        events = conv.recv_until("permission.request", "message.end", "error")
    assert event_names(events)[-1] == "permission.request"  # prompt shown
    assert not (Path(ws) / "out.txt").exists()  # not executed yet
