"""Tests for code-generated image surfacing (inline-images design).

Covers the whole display-only pipeline:

- ``files.images`` — workspace snapshot/diff + the machine marker encode/parse/
  strip.
- ``bash`` tool — detects images the command writes and appends the marker.
- ``graph`` helpers — ``strip_tool_image_markers`` (send-only, hides the marker
  from the model) and ``collect_turn_images`` (current-turn scan for the
  ``additional_kwargs`` anchor).
- ``messages_ui`` — rebuilds ``image`` blocks on history replay from the anchor
  (and the ToolMessage-marker fallback), stripping the marker from tool bubbles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime.files import images as im
from ginno_runtime.files.registry import reset_registries

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_registries()
    yield
    reset_registries()


# ── marker primitives ────────────────────────────────────────────────────────


def test_marker_roundtrip_with_unusual_names(tmp_path: Path):
    paths = [str(tmp_path / "chart 1.png"), str(tmp_path / "趋势图.jpg")]
    marker = im.encode_images_marker(paths)
    assert im.parse_images_marker("prefix\n" + marker) == paths
    assert im.strip_images_marker("output\n" + marker) == "output"


def test_parse_handles_absent_and_nonstr():
    assert im.parse_images_marker("no marker here") == []
    assert im.parse_images_marker(None) == []  # type: ignore[arg-type]
    assert im.parse_images_marker(["list"]) == []  # type: ignore[arg-type]
    assert im.strip_images_marker(None) is None  # type: ignore[arg-type]


def test_parse_rejects_malformed_payload():
    assert im.parse_images_marker("<!--ginno-images:not-json-->") == []
    assert im.parse_images_marker('<!--ginno-images:{"a":1}-->') == []


def test_snapshot_diff_detects_new_modified_and_ignores_nonimage(tmp_path: Path):
    (tmp_path / "keep.png").write_bytes(b"1")
    before = im.snapshot_images(tmp_path)
    (tmp_path / "new.png").write_bytes(b"2")
    (tmp_path / "keep.png").write_bytes(b"11")  # modified
    (tmp_path / "data.csv").write_text("a,b")  # not an image
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.png").write_bytes(b"3")  # skipped dir
    after = im.snapshot_images(tmp_path)
    names = sorted(Path(p).name for p in im.diff_images(before, after))
    assert names == ["keep.png", "new.png"]


# ── bash tool detection ──────────────────────────────────────────────────────


def _bash_tool(workspace: Path):
    from ginno_runtime.tools.builtin import build_builtin_tools

    tools = build_builtin_tools(str(workspace))
    return next(t for t in tools if t.name == "bash")


def test_bash_appends_marker_for_generated_image(tmp_path: Path):
    bash = _bash_tool(tmp_path)
    out = bash.invoke({"command": "printf x > plot.png && echo done"})
    assert "done" in out
    assert im.parse_images_marker(out) == [str(tmp_path / "plot.png")]


def test_bash_no_marker_without_new_image(tmp_path: Path):
    bash = _bash_tool(tmp_path)
    out = bash.invoke({"command": "echo hello"})
    assert im.parse_images_marker(out) == []
    assert "ginno-images" not in out


def test_bash_marker_reflects_only_new_images(tmp_path: Path):
    (tmp_path / "pre.png").write_bytes(b"0")  # exists before the command
    bash = _bash_tool(tmp_path)
    out = bash.invoke({"command": "printf x > post.png"})
    assert im.parse_images_marker(out) == [str(tmp_path / "post.png")]


# ── graph helpers ────────────────────────────────────────────────────────────


def test_strip_tool_image_markers_send_only():
    from ginno_runtime.graph import strip_tool_image_markers

    marker = im.encode_images_marker(["/w/a.png"])
    tm = ToolMessage(content="[exit 0]\nok\n" + marker, tool_call_id="c1")
    hm = HumanMessage(content="hi")
    out = strip_tool_image_markers([hm, tm])
    # input untouched; copy stripped; pairing fields preserved
    assert "ginno-images" in tm.content
    assert out[1].content == "[exit 0]\nok"
    assert out[1].tool_call_id == "c1"
    assert out[0] is hm
    # no markers anywhere -> same list returned
    assert strip_tool_image_markers([hm]) == [hm]


def test_collect_turn_images_stops_at_human():
    from ginno_runtime.graph import collect_turn_images

    m_old = im.encode_images_marker(["/w/old.png"])
    m_new = im.encode_images_marker(["/w/a.png", "/w/b.png"])
    msgs = [
        HumanMessage(content="t1"),
        ToolMessage(content="x\n" + m_old, tool_call_id="c0"),
        HumanMessage(content="t2"),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1"}]),
        ToolMessage(content="y\n" + m_new, tool_call_id="c1"),
    ]
    assert collect_turn_images(msgs) == ["/w/a.png", "/w/b.png"]


# ── messages_ui history replay ───────────────────────────────────────────────


def _conv_with_images(img: Path, anchor: bool):
    marker = im.encode_images_marker([str(img)])
    msgs = [
        HumanMessage(content="plot it", id="t1"),
        AIMessage(
            content="",
            tool_calls=[{"name": "bash", "args": {"command": "p"}, "id": "c1"}],
            id="a1",
        ),
        ToolMessage(content="[exit 0]\nok\n" + marker, tool_call_id="c1"),
        AIMessage(
            content="Here is the chart.",
            additional_kwargs={"ginno_images": [str(img)], "agent_id": "dev"} if anchor else {"agent_id": "dev"},
            id="a2",
        ),
    ]
    return msgs


def _ui(msgs):
    from ginno_runtime.api.messages_ui import _messages_to_ui

    return _messages_to_ui(msgs, "dev", None, project_slug="default", session_id="s1")


def test_history_replay_emits_image_block_and_strips_marker(tmp_path: Path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"data")
    ui = _ui(_conv_with_images(img, anchor=True))
    assistant = [m for m in ui if m["role"] == "assistant"][0]
    kinds = [b["kind"] for b in assistant["blocks"]]
    assert kinds[-1] == "image", "image renders at the end of the bubble"
    # tool bubble must not leak the raw marker
    tool = next(b for b in assistant["blocks"] if b["kind"] == "tool")
    assert "ginno-images" not in tool["content"]
    img_block = assistant["blocks"][-1]
    assert img_block["name"] == "chart.png" and img_block["fileId"]
    # exactly one image block despite anchor + fallback both seeing it
    assert kinds.count("image") == 1


def test_history_replay_fallback_without_anchor(tmp_path: Path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"data")
    ui = _ui(_conv_with_images(img, anchor=False))
    assistant = [m for m in ui if m["role"] == "assistant"][0]
    assert [b["kind"] for b in assistant["blocks"]].count("image") == 1


def test_history_replay_skips_missing_file(tmp_path: Path):
    img = tmp_path / "gone.png"  # never written
    img.write_bytes(b"x")
    msgs = _conv_with_images(img, anchor=True)
    img.unlink()
    ui = _ui(msgs)
    assistant = [m for m in ui if m["role"] == "assistant"][0]
    assert all(b["kind"] != "image" for b in assistant["blocks"])
