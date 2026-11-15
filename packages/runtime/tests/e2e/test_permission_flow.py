"""WebSocket E2E: permission ask -> interrupt -> resume (allow / deny).

write_file is "ask" in the seeded default policy, so scripting a write_file tool
call drives the graph into an interrupt(); the WS layer surfaces it as a
permission.request and waits for a permission_response to resume.

Since plan F1 the tools run in the session workspace (the per-session files
dir) — the model never passes a workspace argument.
"""

from __future__ import annotations

import pytest
from conftest import event_names, events_of

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def test_permission_allow_executes_tool(create_session, ws_conv):
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hello"})]),
        script(text="Wrote it."),
    ]
    sid = create_session(model)

    with ws_conv(sid) as conv:
        conv.invoke("please write a file")
        first = conv.recv_until("permission.request", "message.end", "error")
        names = event_names(first)
        # the turn pauses on a permission request (no message.end yet)
        assert "permission.request" in names
        assert "message.end" not in names
        perm = events_of(first, "permission.request")[0]
        assert perm["tool"] == "write_file"

        conv.respond_permission("allow")
        rest = conv.recv_until("message.end", "error")

    rest_names = event_names(rest)
    assert "tool.end" in rest_names  # the write_file result bubble
    assert "message.end" in rest_names
    assert (paths.session_files_dir("default", sid) / "out.txt").read_text() == "hello"


def test_permission_deny_blocks_tool(create_session, ws_conv):
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hello"})]),
        script(text="Understood, skipping the write."),
    ]
    sid = create_session(model)

    with ws_conv(sid) as conv:
        conv.invoke("please write a file")
        first = conv.recv_until("permission.request")
        conv.respond_permission("deny")
        rest = conv.recv_until("message.end", "error")

    # denied tool resolves as a blocked tool.end and the file is never written
    tool_ends = events_of(rest, "tool.end")
    denied = [e for e in tool_ends if "denied" in (e.get("content", "")).lower()]
    assert denied
    # 2026-11-15 回归：被拦工具的 tool.end **必须带配对的 tool_call_id**。
    # 此前 engine.py 的 BLOCK_PREFIX 分支只从 content 字符串抠出 name 就 emit，
    # 把 _blocked_tool_msg 早已放进 ToolMessage 的 id 丢了 → 前端退化到「按工具名
    # 匹配」，而那个匹配不要求 pending、又从数组头取第一个：denial 文案覆盖到
    # **前面某个已完成的同名工具气泡**上，真正被拦的那个永远停在「执行命令中…」。
    # 同名工具一个 turn 内多次调用（bash 重试最常见）即触发。
    starts = [e for e in events_of(first, "tool.start") if e.get("name") == "write_file"]
    assert starts, "write_file 应当先有 tool.start"
    assert denied[0].get("id") == starts[0].get("id"), (
        "被拦工具的 tool.end 必须带上配对的 tool_call_id："
        f"tool.start={starts[0].get('id')!r} tool.end={denied[0].get('id')!r}"
    )
    assert not (paths.session_files_dir("default", sid) / "out.txt").exists()
    assert "message.end" in event_names(rest)


def test_allowed_tool_needs_no_permission(create_session, ws_conv):
    # read_file is in the default allow list -> runs without a permission prompt
    model = [
        script(tool_calls=[script_tool_call("read_file", {"path": "in.txt"})]),
        script(text="got it"),
    ]
    sid = create_session(model)
    # fixture file goes into the session workspace (the tools' bound cwd)
    (paths.session_files_dir("default", sid) / "in.txt").write_text("payload")
    with ws_conv(sid) as conv:
        conv.invoke("read it")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert "permission.request" not in names
    assert "tool.end" in names
    assert "message.end" in names
