"""History paging uses real checkpoints and preserves the full replay contract."""

import uuid
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.files.images import encode_images_marker
from ginno_runtime.microcompact import CLEARED_TOOL_OUTPUT
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


def _session(client, patch_build_model, workspace):
    patch_build_model(script(text="ok"))
    return client.post("/api/sessions", json={
        "project_slug": "default", "workspace": str(workspace), "agent_id": "dev",
    }).json()["id"]


def _save(sid, messages):
    FileCheckpointer("default").put(
        {"configurable": {"thread_id": sid}},
        {
            "v": 4, "id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(),
            "channel_values": {"messages": messages}, "channel_versions": {"messages": 1},
            "versions_seen": {}, "updated_channels": ["messages"],
        },
        {"source": "loop", "step": 1, "parents": {}}, {"messages": 1},
    )


def _turn(i, body=None):
    messages = [HumanMessage(id=f"u-{i}", content=f"Question {i}")]
    if body is not None:
        messages.extend([
            AIMessage(id=f"tool-{i}", content="", tool_calls=[{
                "name": "bash", "args": {"command": "printf fixture"}, "id": f"call-{i}",
            }]),
            ToolMessage(id=f"result-{i}", tool_call_id=f"call-{i}", content=body),
        ])
    return [*messages, AIMessage(id=f"a-{i}", content=f"Answer {i}")]


def _ids(rows):
    return [row["id"] for row in rows]


def test_640_turns_short_page_and_whole_export(client, patch_build_model, tmp_path):
    sid = _session(client, patch_build_model, tmp_path)
    body = "[exit 0]\n" + "synthetic output fragment\n" * 235 + "tail beyond 4000"
    _save(sid, [message for i in range(640) for message in _turn(i, body)])
    endpoint = f"/api/sessions/{sid}/history"
    full = client.get(endpoint)
    short = client.get(endpoint, params={"turns": 40})
    assert set(full.json()) == {"ok", "messages", "last_error"}
    assert (len(full.json()["messages"]), len(short.json()["messages"])) == (1280, 80)
    assert len(short.content) <= len(full.content) / 10
    print(f"640 turns: full={len(full.content)} B short={len(short.content)} B")
    all_ids = []
    page = short.json()
    while True:
        all_ids[:0] = _ids(page["messages"])
        if not page["page"]["before"]:
            break
        page = client.get(endpoint, params={
            "turns": 40, "before": page["page"]["before"], "revision": page["page"]["revision"],
        }).json()
        assert page["page"]["mode"] == "prepend"
    assert all_ids == _ids(full.json()["messages"])
    block = short.json()["messages"][1]["blocks"][0]
    assert block["id"] == "call-600"
    assert block["content"] == "[exit 0]\n"
    key = block["detail"]["key"]
    assert client.get(f"{endpoint}/tools/{key}").json()["content"] == body
    other = _session(client, patch_build_model, tmp_path)
    assert client.get(f"/api/sessions/{other}/history/tools/{key}").status_code == 404
    assert client.get(f"/api/sessions/{other}/history").json() == {"ok": True, "messages": []}
    empty = client.get(f"/api/sessions/{other}/history?turns=40").json()
    assert empty["messages"] == [] and empty["page"]["before"] is None
    exported = client.get(f"/api/export/session/{sid}?format=md").text
    assert "Question 0" in exported and "Answer 639" in exported
    assert exported.count("tail beyond 4000") == 640


def test_pages_follow_projected_turns_and_checkpoint_changes(client, patch_build_model, tmp_path):
    sid = _session(client, patch_build_model, tmp_path)
    url = f"/api/sessions/{sid}/history"
    report = HumanMessage(
        content='<ginno_plan_update plan="audit">Report sentinel</ginno_plan_update>',
        additional_kwargs={"ginno_subagent_result": "plan-audit"},
    )
    messages = [
        *_turn(0, CLEARED_TOOL_OUTPUT),
        HumanMessage(content="Nameless question"), AIMessage(content="Nameless answer"),
        report, AIMessage(id="report-reply", content="Report reply"), *_turn(2), *_turn(3),
    ]
    _save(sid, messages)
    latest = client.get(url, params={"turns": 2}).json()
    assert _ids(latest["messages"]) == ["u-2", "a-2", "u-3", "a-3"]
    assert latest["page"]["notice"]["id"].startswith("microcompact:")
    previous = client.get(url, params={
        "turns": 2, "before": latest["page"]["before"], "revision": latest["page"]["revision"],
    }).json()
    assert _ids(previous["messages"])[-1] == "report-reply"
    assert "Report sentinel" in str(previous["messages"][-1])
    assert all(row_id.startswith("history-") for row_id in _ids(previous["messages"][:2]))
    index = client.get(url, params={"index": True}).json()["messages"]
    assert _ids(index[3:6]) == _ids(previous["messages"])
    # Append while an older-page cursor is in flight. The response includes
    # the changed tail so a reader cannot construct a mixed transcript.
    _save(sid, [*messages, *_turn(4)])
    repaired = client.get(url, params={
        "turns": 2, "before": latest["page"]["before"], "revision": latest["page"]["revision"],
    }).json()
    assert repaired["page"]["mode"] == "replace"
    assert _ids(repaired["messages"]) == _ids(previous["messages"]) + _ids(latest["messages"]) + ["u-4", "a-4"]
    refreshed = client.get(url, params={"turns": 2, "refresh": latest["page"]["tail"]}).json()
    assert refreshed["page"]["mode"] == "refresh"
    assert _ids(refreshed["messages"]) == ["u-2", "a-2", "u-3", "a-3", "u-4", "a-4"]
    # Compaction removes the old prefix. A surviving coverage anchor rebuilds
    # the full loaded span with the new checkpoint and a fresh notice state.
    _save(sid, [*_turn(2), *_turn(3), *_turn(4), *_turn(5), *_turn(6)])
    compacted = client.get(url, params={
        "turns": 2, "refresh": latest["page"]["tail"], "coverage": "u-2",
    }).json()
    assert compacted["page"]["mode"] == "replace" and compacted["page"]["notice"] is None
    assert _ids(compacted["messages"]) == _ids(client.get(url).json()["messages"])


@pytest.mark.parametrize("anchor", [False, True])
def test_preview_preserves_diff_image_mermaid_and_question(client, patch_build_model, tmp_path, anchor):
    sid = _session(client, patch_build_model, tmp_path)
    path = tmp_path / "graph.png"
    path.write_bytes(b"synthetic png")
    marker = encode_images_marker([str(path)])
    diagram = "```mermaid\ngraph TD\n  A --> B\n```"
    _save(sid, [
        HumanMessage(id="user", content="Plot this"),
        AIMessage(id="assistant", content="", tool_calls=[
            {"name": "bash", "args": {"command": "plot"}, "id": "plot"},
            {"name": "edit_file", "args": {"path": "x.py", "old": "a\n", "new": "b\n"}, "id": "edit"},
            {"name": "ask_user", "args": {"question": "Ok?", "options": ["yes", "no"]}, "id": "question"},
        ]),
        ToolMessage(id="plot-result", tool_call_id="plot", content="[exit 0]\nfigure\n" + marker),
        ToolMessage(id="edit-result", tool_call_id="edit", content="Edited."),
        AIMessage(id="last-step", content=diagram,
                  additional_kwargs={"ginno_images": [str(path)]} if anchor else {}),
    ])
    url = f"/api/sessions/{sid}/history"
    result = client.get(url, params={"turns": 1, "target": "edit-result"}).json()
    assert result["page"]["target"] == {"id": "assistant", "block": 1}
    blocks = result["messages"][1]["blocks"]
    assert [block["kind"] for block in blocks] == ["tool", "tool", "question", "text", "image"]
    assert blocks[1]["diff"]["added"] == blocks[1]["diff"]["removed"] == 1
    assert blocks[1]["diff"]["text"] == "" and blocks[3]["text"] == diagram
    assert blocks[4]["fileId"]
    detail = client.get(f"{url}/tools/{blocks[1]['detail']['key']}").json()
    assert detail["content"] == "Edited." and "-a\n+b" in detail["diff"]["text"]
    assert "ginno-images" not in client.get(f"{url}/tools/{blocks[0]['detail']['key']}").json()["content"]
