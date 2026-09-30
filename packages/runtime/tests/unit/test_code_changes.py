"""Agent file-change transport for the code panel (S3, brief §1 / §3.1).

The whole pipeline, one layer at a time — each test pins a rule from the brief
rather than a code path:

* ``files.code_changes`` — encode / parse / strip round-trip, malformed input,
  CJK + spaces in paths, longest-prefix root matching.
* ``write_file`` / ``edit_file`` — the marker is actually appended, carries the
  right ``op``, and its ``version`` equals what the code panel's own ``read``
  returns (brief §0: without that identity the pushed change could not reuse the
  S2 conflict bar).
* ``graph.strip_tool_code_markers`` — the model never sees the marker, while the
  persisted ToolMessage keeps it.
* ``api.messages_ui._tool_content_str`` — the replayed history bubble AND the
  live ``tool.end`` payload (stream.py renders through this same helper) carry
  no marker.
* ``api.stream._emit_code_changes`` — parses the marker off a real tool result
  and emits one ``code.changed`` per file with the right payload.

The agent node's copy-only semantics are the same ones
``tests/unit/test_generated_images.py`` pins for the image marker; this file
mirrors its shape deliberately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.api import stream as stream_api
from ginno_runtime.files import code_changes as cc
from ginno_runtime.files.registry import reset_registries
from ginno_runtime.server_shared import _SESSIONS
from ginno_runtime.testing.fake_model import ScriptedChatModel
from ginno_runtime.tools.builtin import build_builtin_tools

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-code-changes"


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_registries()
    yield
    reset_registries()


@pytest.fixture
def session_ws():
    """A warm session whose workspace is the only root (plus its ``rw`` mount)."""
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    _SESSIONS[SID] = {
        "session_id": SID,
        "project_slug": SLUG,
        "workspace": str(ws),
    }
    try:
        yield ws
    finally:
        _SESSIONS.pop(SID, None)


def _tool(name: str, workspace: Path):
    return next(t for t in build_builtin_tools(str(workspace)) if t.name == name)


def _change(path: str, op: str = "write", version: str = "12:34567") -> dict:
    return {"path": path, "op": op, "version": version}


# ── marker primitives ────────────────────────────────────────────────────────


def test_marker_roundtrip_with_multibyte_and_spacey_paths(tmp_path: Path):
    changes = [
        _change(str(tmp_path / "有 空格 的 目录" / "主线.py")),
        _change(str(tmp_path / "ünïcode-路径" / "ファイル.md"), op="edit", version="7:1"),
    ]
    marker = cc.encode_code_marker(changes)
    assert cc.parse_code_marker("ok\n" + marker) == changes
    assert cc.strip_code_marker("ok\n" + marker) == "ok"
    # absolute paths must survive the JSON hop byte-for-byte
    assert "有 空格 的 目录" in marker


def test_strip_returns_the_original_text_untouched():
    """Exactly the shape the tool builds: result + "\\n" + marker."""
    original = "wrote 12 bytes to /w/x.py"
    out = original + "\n" + cc.encode_code_marker([_change("/w/x.py")])
    assert cc.strip_code_marker(out) == original
    assert "ginno-code" not in cc.strip_code_marker(out)


def test_parse_handles_absent_empty_and_nonstr():
    assert cc.parse_code_marker("no marker here") == []
    assert cc.parse_code_marker("") == []
    assert cc.parse_code_marker(None) == []  # type: ignore[arg-type]
    assert cc.parse_code_marker(["list"]) == []  # type: ignore[arg-type]
    assert cc.parse_code_marker("<!--ginno-code:[]-->") == []
    assert cc.strip_code_marker(None) is None  # type: ignore[arg-type]
    assert cc.strip_code_marker("plain") == "plain"


def test_parse_rejects_malformed_payload():
    # matches the wrapper but the payload is not JSON
    assert cc.parse_code_marker("<!--ginno-code:[{bad}]-->") == []
    # non-list JSON
    assert cc.parse_code_marker('<!--ginno-code:["/a.py"]-->') == []
    # entries missing path / with an unknown op are dropped
    assert cc.parse_code_marker(
        '<!--ginno-code:[{}, {"path":""}, {"path":"/a"}, {"path":"/a","op":"chmod"}]-->'
    ) == []
    mixed = (
        "<!--ginno-code:["
        '{"path":"/good.py","op":"edit","version":"1:2"},'
        '{"path":"","op":"write","version":"1:2"},'
        '{"path":"/no-op.py"},'
        '{"path":"/bad-op.py","op":"rm","version":"1:2"},'
        '"nope"'
        "]-->"
    )
    assert cc.parse_code_marker(mixed) == [
        {"path": "/good.py", "op": "edit", "version": "1:2"}
    ]


def test_parse_marker_is_not_greedy_past_the_first_close():
    a = cc.encode_code_marker([_change("/a.py")])
    b = cc.encode_code_marker([_change("/b.py")])
    assert [c["path"] for c in cc.parse_code_marker(a + "\n" + b)] == ["/a.py"]


# ── root matching ────────────────────────────────────────────────────────────


def test_match_root_id_longest_prefix_wins(tmp_path: Path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    roots = [
        {"id": "session", "path": str(tmp_path / "ws")},
        {"id": "f_outer", "path": str(outer)},
        {"id": "f_inner", "path": str(inner)},
    ]
    assert cc.match_root_id(str(inner / "deep" / "x.py"), roots) == "f_inner"
    assert cc.match_root_id(str(outer / "y.py"), roots) == "f_outer"
    assert cc.match_root_id(str(outer), roots) == "f_outer"
    # a sibling whose name merely starts with the root's name is NOT inside it
    assert cc.match_root_id(str(tmp_path / "outer-two" / "z.py"), roots) is None
    assert cc.match_root_id(str(tmp_path / "elsewhere" / "z.py"), roots) is None
    assert cc.match_root_id("", roots) is None
    assert cc.match_root_id(str(inner / "x"), []) is None
    assert cc.match_root_id(str(inner / "x"), [{"id": "nopath"}]) is None


def test_match_root_id_tolerates_macos_tmp_symlink(tmp_path: Path):
    """A path spelled through a symlink still matches the real root (macOS /tmp)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert cc.match_root_id(str(link / "x.py"), [{"id": "r", "path": str(real)}]) == "r"


# ── tool side: the marker is appended, with op + post-write version ──────────


def test_write_file_appends_parseable_marker(session_ws: Path):
    content = "print('hi')\n"
    out = _tool("write_file", session_ws).invoke({"path": "probe.py", "content": content})
    changes = cc.parse_code_marker(out)
    assert len(changes) == 1
    assert changes[0]["op"] == "write"
    target = (session_ws / "probe.py").resolve()
    assert changes[0]["path"] == str(target)
    st = target.stat()
    assert changes[0]["version"] == f"{st.st_size}:{st.st_mtime_ns}"
    # stripping leaves exactly the tool's own result text, no residue
    assert cc.strip_code_marker(out) == f"wrote {len(content)} bytes to {target}"


def test_edit_file_appends_marker_with_op_edit(session_ws: Path):
    (session_ws / "probe.py").write_text("old line\n", encoding="utf-8")
    out = _tool("edit_file", session_ws).invoke(
        {"path": "probe.py", "old": "old line", "new": "new line"}
    )
    changes = cc.parse_code_marker(out)
    assert len(changes) == 1 and changes[0]["op"] == "edit"
    target = (session_ws / "probe.py").resolve()
    assert changes[0]["path"] == str(target)
    st = target.stat()
    assert changes[0]["version"] == f"{st.st_size}:{st.st_mtime_ns}"
    assert cc.strip_code_marker(out) == "ok"


def test_error_results_carry_no_marker(session_ws: Path):
    """An edit that does not apply announces nothing — there is no new version."""
    edit = _tool("edit_file", session_ws)
    (session_ws / "probe.py").write_text("only one\n", encoding="utf-8")
    for args in (
        {"path": "missing.py", "old": "a", "new": "b"},
        {"path": "probe.py", "old": "nope", "new": "x"},
    ):
        out = edit.invoke(args)
        assert out.startswith("[error]")
        assert cc.parse_code_marker(out) == []


def test_marker_path_roundtrips_cjk_filename(session_ws: Path):
    out = _tool("write_file", session_ws).invoke(
        {"path": "中文 目录/文件 名.py", "content": "x = 1\n"}
    )
    changes = cc.parse_code_marker(out)
    assert len(changes) == 1
    assert Path(changes[0]["path"]).name == "文件 名.py"
    assert Path(changes[0]["path"]).is_file()


def test_pushed_version_equals_what_the_code_panel_reads(session_ws: Path):
    """Brief §0: the pushed version must drive S2's conflict bar unchanged."""
    out = _tool("write_file", session_ws).invoke({"path": "probe.py", "content": "x = 1\n"})
    pushed = cc.parse_code_marker(out)[0]["version"]

    app = FastAPI()
    app.include_router(code_panel.router)
    c = TestClient(app)
    body = c.get(
        "/api/code/read",
        params={
            "project_slug": SLUG,
            "session_id": SID,
            "root": "session",
            "path": "probe.py",
        },
    ).json()
    assert body["ok"] is True
    assert body["version"] == pushed


# ── model view: stripped, persisted kept ─────────────────────────────────────


def test_strip_tool_code_markers_is_send_only():
    from ginno_runtime.graph import strip_tool_code_markers

    marker = cc.encode_code_marker([_change("/w/a.py", op="edit")])
    tm = ToolMessage(content="ok\n" + marker, tool_call_id="c1")
    hm = HumanMessage(content="hi")
    out = strip_tool_code_markers([hm, tm])
    assert "ginno-code" in tm.content  # input untouched (persisted copy)
    assert out[1].content == "ok"
    assert out[1].tool_call_id == "c1"
    assert out[0] is hm
    # nothing to strip -> the same list comes back (no churn)
    assert strip_tool_code_markers([hm]) == [hm]


def test_agent_node_history_has_no_marker_and_keeps_tool_pairing(session_ws: Path):
    """The trailer is invisible to the model, the tool result text is intact."""
    from ginno_runtime.graph import strip_tool_code_markers

    raw = _tool("write_file", session_ws).invoke({"path": "probe.py", "content": "x = 1\n"})
    msgs = [
        HumanMessage(content="write it"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {}, "id": "c1"}]),
        ToolMessage(content=raw, tool_call_id="c1"),
    ]
    sent = strip_tool_code_markers(msgs)
    assert "ginno-code" not in sent[2].content
    assert sent[2].content.startswith("wrote ") and "probe.py" in sent[2].content


# ── UI render: history replay + live tool.end (same helper) ──────────────────


def test_tool_content_str_strips_the_marker():
    from ginno_runtime.api.messages_ui import _tool_content_str

    raw = "ok\n" + cc.encode_code_marker([_change("/w/a.py")])
    assert _tool_content_str(raw) == "ok"
    # provider-block form (list content) goes through the same strip
    assert _tool_content_str([{"type": "text", "text": raw}]) == "ok"


def test_history_replay_tool_bubble_hides_the_marker(session_ws: Path):
    from ginno_runtime.api.messages_ui import _messages_to_ui

    raw = _tool("write_file", session_ws).invoke({"path": "probe.py", "content": "x = 1\n"})
    msgs = [
        HumanMessage(content="write it", id="t1"),
        AIMessage(
            content="",
            tool_calls=[{"name": "write_file", "args": {"path": "probe.py"}, "id": "c1"}],
            id="a1",
        ),
        ToolMessage(content=raw, tool_call_id="c1"),
    ]
    ui = _messages_to_ui(msgs, "dev", None, project_slug=SLUG, session_id=SID)
    assistant = [m for m in ui if m["role"] == "assistant"][0]
    tools = [b for b in assistant["blocks"] if b["kind"] == "tool"]
    assert tools and all("ginno-code" not in t["content"] for t in tools)


# ── WS emit ──────────────────────────────────────────────────────────────────


class _Emitter:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def safe_send(self, obj) -> None:
        self.sent.append(obj)

    @staticmethod
    def emit(name: str, payload: dict) -> dict:
        return {"event": name, **payload}


async def test_emit_code_changes_payload_and_root_id(session_ws: Path):
    raw = _tool("write_file", session_ws).invoke({"path": "sub/a.py", "content": "y = 2\n"})
    e = _Emitter()
    await stream_api._emit_code_changes(e.safe_send, e.emit, SLUG, SID, raw)

    assert len(e.sent) == 1
    ev = e.sent[0]
    assert ev["event"] == "code.changed"
    assert ev["session_id"] == SID
    assert ev["op"] == "write"
    assert ev["root_id"] == "session"
    assert ev["path"] == str((session_ws / "sub" / "a.py").resolve())
    # the emitted version is the marker's, i.e. the panel's version algorithm
    st = Path(ev["path"]).stat()
    assert ev["version"] == f"{st.st_size}:{st.st_mtime_ns}"


async def test_emit_code_changes_resolves_a_mount_root(session_ws: Path, tmp_path_factory):
    mount = tmp_path_factory.mktemp("code-mount")
    _SESSIONS[SID]["context_dirs"] = [
        {"id": "f_mount", "path": str(mount), "name": "mount", "access": "rw"}
    ]
    target = mount / "m.py"
    target.write_text("m = 1\n", encoding="utf-8")
    st = target.stat()
    raw = cc.encode_code_marker(
        [{"path": str(target), "op": "edit", "version": f"{st.st_size}:{st.st_mtime_ns}"}]
    )
    e = _Emitter()
    await stream_api._emit_code_changes(e.safe_send, e.emit, SLUG, SID, raw)
    assert e.sent[0]["root_id"] == "f_mount"
    assert e.sent[0]["op"] == "edit"


async def test_emit_code_changes_outside_every_root_is_null(session_ws: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.py"
    raw = "ok\n" + cc.encode_code_marker([_change(str(outside))])
    e = _Emitter()
    await stream_api._emit_code_changes(e.safe_send, e.emit, SLUG, SID, raw)
    assert len(e.sent) == 1
    assert e.sent[0]["root_id"] is None  # placeable nowhere -> null, never an error


async def test_emit_code_changes_emits_one_event_per_file(session_ws: Path):
    raw = cc.encode_code_marker([_change(str(session_ws / "a.py"), op="write"),
                                _change(str(session_ws / "b.py"), op="edit")])
    e = _Emitter()
    await stream_api._emit_code_changes(e.safe_send, e.emit, SLUG, SID, raw)
    assert [ev["op"] for ev in e.sent] == ["write", "edit"]
    assert [Path(ev["path"]).name for ev in e.sent] == ["a.py", "b.py"]


async def test_emit_code_changes_is_a_noop_without_a_marker(session_ws: Path):
    e = _Emitter()
    await stream_api._emit_code_changes(e.safe_send, e.emit, SLUG, SID, "[exit 0]\nhello")
    # an unknown session must not stop the event either — root_id just goes null
    await stream_api._emit_code_changes(
        e.safe_send, e.emit, SLUG, "nope", "ok\n" + cc.encode_code_marker([_change("/x.py")])
    )
    assert [ev["event"] for ev in e.sent] == ["code.changed"]
    assert e.sent[0]["root_id"] is None
    assert e.sent[0]["session_id"] == "nope"


async def test_emit_code_changes_survives_a_broken_root_lookup(
    session_ws: Path, monkeypatch: pytest.MonkeyPatch
):
    """Decoration must never break a turn: a raising lookup still emits."""
    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(code_panel, "_list_roots", _boom)
    e = _Emitter()
    await stream_api._emit_code_changes(
        e.safe_send, e.emit, SLUG, SID, "ok\n" + cc.encode_code_marker([_change("/x.py")])
    )
    assert len(e.sent) == 1 and e.sent[0]["root_id"] is None


# ── the real thing: one turn through the compiled graph ──────────────────────


class _RecordingModel(ScriptedChatModel):
    """ScriptedChatModel that records every LLM call's message list."""

    recorded: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.recorded.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


async def test_one_real_turn_end_to_end(session_ws: Path):
    """The brief's W1 acceptance: a REAL ``write_file`` call, end to end.

    Drives the compiled graph with the scripted model so the tool actually runs
    and the marker actually lands in the checkpoint, then proves all three
    links: the model's copy is clean, the persisted ToolMessage still carries the
    trailer, and the trailer parses into the exact ``code.changed`` payload.
    """
    from langchain_core.messages import ToolMessage as _ToolMessage

    from ginno_runtime.checkpointer import FileCheckpointer
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import script, script_tool_call

    paths.ensure_layout()
    model = _RecordingModel(
        scripts=[
            script(
                tool_calls=[
                    script_tool_call(
                        "write_file", {"path": "probe.py", "content": "x = 1\n"}
                    )
                ]
            ),
            script(text="done"),
        ]
    )
    graph = build_graph(model=model, project_slug=SLUG, workspace=str(session_ws), mcp_tools=[])
    cfg = {"configurable": {"thread_id": "cc-e2e", "project_slug": SLUG, "agent_id": "dev"}}
    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="write probe.py")],
            "workspace": str(session_ws),
            "project_slug": SLUG,
            "agent_id": "dev",
            "active_skills": [],
            "pending_tool_calls": [],
        },
        config=cfg,
    )

    # 1. the file is really on disk, and the FINAL LLM call saw no marker
    assert (session_ws / "probe.py").read_text(encoding="utf-8") == "x = 1\n"
    model_view = json.dumps(
        [getattr(m, "content", None) for m in model.recorded[-1]], ensure_ascii=False, default=str
    )
    assert "ginno-code" not in model_view, "the model must never see its own marker"
    assert "probe.py" in model_view, "the tool result itself still reaches the model"

    # 2. the persisted ToolMessage keeps the trailer (the WS layer's source)
    stored = FileCheckpointer(project_slug=SLUG).get_tuple(
        {"configurable": {"thread_id": "cc-e2e"}}
    ).checkpoint["channel_values"]["messages"]
    tool_msg = next(m for m in stored if isinstance(m, _ToolMessage))
    assert "<!--ginno-code:" in tool_msg.content

    # 3. that exact text yields the wire payload
    e = _Emitter()
    await stream_api._emit_code_changes(e.safe_send, e.emit, SLUG, SID, tool_msg.content)
    assert len(e.sent) == 1
    ev = e.sent[0]
    assert ev["event"] == "code.changed"
    assert ev["op"] == "write" and ev["root_id"] == "session"
    assert ev["path"] == str((session_ws / "probe.py").resolve())
    st = Path(ev["path"]).stat()
    assert ev["version"] == f"{st.st_size}:{st.st_mtime_ns}"