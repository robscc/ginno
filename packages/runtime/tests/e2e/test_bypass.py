"""E2E: privileged (bypass_permissions) mode lets every tool through, no prompt.

Since plan F1 the tools run in the session workspace (the per-session files
dir) — the model never passes a workspace argument.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import event_names
from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def _set_bypass(isolated_home: Path, value: bool) -> None:
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = value
    sp.write_text(json.dumps(s))


def test_bypass_on_executes_write_without_prompt(client, create_session, ws_conv, isolated_home):
    _set_bypass(isolated_home, True)  # override the test-default (False)
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hi"})]),
            script(text="wrote it"),
        ]
    )
    with ws_conv(sid) as conv:
        conv.invoke("write a file")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert "permission.request" not in names  # privileged → no prompt
    assert "tool.end" in names
    # actually ran — in the session workspace
    out = paths.session_files_dir("default", sid) / "out.txt"
    assert out.read_text(encoding="utf-8") == "hi"


def test_bypass_off_asks_for_write(client, create_session, ws_conv, isolated_home):
    _set_bypass(isolated_home, False)
    sid = create_session(
        [
            script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hi"})]),
            script(text="wrote it"),
        ]
    )
    with ws_conv(sid) as conv:
        conv.invoke("write a file")
        events = conv.recv_until("permission.request", "message.end", "error")
    assert event_names(events)[-1] == "permission.request"  # prompt shown
    assert not (paths.session_files_dir("default", sid) / "out.txt").exists()  # not executed yet
