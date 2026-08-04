"""API/E2E tests: WS wiring for attached files + reactive preview refresh.

Covers: invoke with files → attachment context reaches the model; history
carries file blocks; analyze_table derived result → preview.emit + artifact
with session_id; write_file touch → preview.invalidate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime.files import reset_registries
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _fresh_files(isolated_home):
    reset_registries()
    yield
    reset_registries()


@pytest.fixture(autouse=True)
def _bypass_on(client, isolated_home):
    """These tests exercise file wiring, not the permission flow — let tools run.

    Depends on ``client`` so the lifespan has seeded settings.json first."""
    import json

    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(json.dumps(s))


@pytest.fixture
def ws_dir(isolated_home) -> Path:
    ws = isolated_home / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _csv(dirpath: Path, name="data.csv", body="a,b\n1,2\n3,4\n") -> Path:
    f = dirpath / name
    f.write_text(body, encoding="utf-8")
    return f


def _upload(client, sid: str, name: str, data: bytes, mime="text/csv") -> dict:
    r = client.post(
        "/api/files", data={"session_id": sid}, files={"file": (name, data, mime)}
    ).json()
    assert r["ok"] is True, r
    return r["file"]


def test_invoke_files_inject_attachment_context(client, create_session, ws_conv, ws_dir):
    """The model sees the attached file (path + schema) in the turn-context
    message (plan B1 — volatile content no longer rides the system prompt)."""
    seen_prompts: list[str] = []
    seen_humans: list[str] = []

    class EchoModel:
        """Captures the system prompt + human messages, answers plainly."""

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kw):
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

            for m in messages:
                if isinstance(m, SystemMessage):
                    seen_prompts.append(str(m.content))
                elif isinstance(m, HumanMessage):
                    seen_humans.append(str(m.content))
            return AIMessage(content="已收到文件。")

    sid = create_session(EchoModel(), workspace=str(ws_dir))
    f = _csv(ws_dir)
    up = _upload(client, sid, "data.csv", f.read_bytes())

    with ws_conv(sid) as conv:
        conv.send(
            {
                "type": "invoke",
                "message": "分析这个文件",
                "files": [{"id": up["id"]}],
            }
        )
        conv.recv_until("message.end", "error")

    assert seen_prompts, "model was not invoked"
    from ginno_runtime.world_state import TURN_CONTEXT_PREFIX

    turn_ctx = next(
        (h for h in reversed(seen_humans) if h.startswith(TURN_CONTEXT_PREFIX)), ""
    )
    assert turn_ctx, "no turn-context message reached the model"
    assert "attached_files" in turn_ctx
    assert "data.csv" in turn_ctx
    assert up["path"] in turn_ctx  # the uploaded (registry-canonical) path
    assert "analyze_table" in turn_ctx  # steering guidance
    assert "a(object)" in turn_ctx or "a(" in turn_ctx  # schema summary present
    # stable system layer stays free of per-turn attachments (B2)
    assert "attached_files" not in seen_prompts[0]


def test_invoke_files_default_intent_when_no_text(client, create_session, ws_conv, ws_dir):
    sid = create_session(script(text="好的，这是概览。"), workspace=str(ws_dir))
    f = _csv(ws_dir)
    up = _upload(client, sid, "data.csv", f.read_bytes())
    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "", "files": [{"id": up["id"]}]})
        events = conv.recv_until("message.end", "error")
    assert "error" not in [e["event"] for e in events]
    # history user bubble carries BOTH the file chip and the synthesized text
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    user = msgs[0]
    kinds = [b["kind"] for b in user["blocks"]]
    assert "file" in kinds and "text" in kinds
    fb = next(b for b in user["blocks"] if b["kind"] == "file")
    assert fb["name"] == "data.csv" and fb["fileId"] == up["id"]


def test_history_carries_file_blocks(client, create_session, ws_conv, ws_dir):
    sid = create_session(script(text="看到了。"), workspace=str(ws_dir))
    f = _csv(ws_dir)
    with ws_conv(sid) as conv:
        # path-based attachment (no prior upload) also works and auto-registers
        conv.send(
            {
                "type": "invoke",
                "message": "看看这个",
                "files": [{"name": "data.csv", "path": str(f)}],
            }
        )
        conv.recv_until("message.end", "error")
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    fb = next(b for b in msgs[0]["blocks"] if b["kind"] == "file")
    assert fb["name"] == "data.csv"
    assert fb["fileKind"] == "table"
    # auto-registered as an artifact with session attribution
    arts = client.get("/api/artifacts?project_slug=default").json()
    assert any(a["kind"] == "file" and a["session_id"] == sid for a in arts)


def test_analyze_table_derived_result_emits_preview_open(client, create_session, ws_conv, ws_dir):
    """analyze_table with a DataFrame result → preview.emit {open:true} +
    artifact registered (the 'result auto-opens' moment)."""
    f = _csv(ws_dir, name="sales.csv", body="地区,金额\n北,100\n南,300\n")
    code = "result = df.groupby('地区')['金额'].sum().reset_index()"
    scripted = [
        script(
            tool_calls=[
                script_tool_call("analyze_table", {"path": str(f), "code": code})
            ]
        ),
        script(text="分析完成。"),
    ]
    sid = create_session(scripted, workspace=str(ws_dir))
    up = _upload(client, sid, "sales.csv", f.read_bytes())

    with ws_conv(sid) as conv:
        conv.send(
            {
                "type": "invoke",
                "message": "按地区汇总",
                "files": [{"id": up["id"]}],
            }
        )
        events = conv.recv_until("message.end", "error")

    emits = [e for e in events if e["event"] == "preview.emit"]
    assert emits, f"no preview.emit in {[e['event'] for e in events]}"
    assert emits[0]["open"] is True
    assert emits[0]["name"].startswith("sales-result-")
    # derived file is previewable through the API
    pv = client.get(f"/api/files/{emits[0]['file_id']}/preview").json()
    assert pv["ok"] is True
    assert pv["rows"]  # has data
    # and registered as a session artifact, relocated into the session's
    # results/ dir (not left next to the source file)
    arts = client.get("/api/artifacts?project_slug=default").json()
    derived = [
        a
        for a in arts
        if a.get("ref", "").endswith(".csv")
        and a.get("session_id") == sid
        and "results" in a.get("ref", "")
    ]
    assert derived, f"no derived artifact in {arts}"
    assert f"/sessions/{sid}/results/" in derived[0]["ref"], derived[0]["ref"]
    # physical file actually lives there
    from pathlib import Path

    assert Path(derived[0]["ref"]).is_file()
    # scoped artifacts query returns it too
    scoped = client.get(f"/api/artifacts?project_slug=default&session_id={sid}").json()
    assert any(a["id"] == derived[0]["id"] for a in scoped)


def test_write_file_touch_emits_invalidate(client, create_session, ws_conv, ws_dir):
    """Editing a registered file via write_file → preview.invalidate for it.

    Attach by PATH (auto-registers in the registry at f.resolve()); the agent
    then overwrites that same path, so the touch matches the registered entry.
    """
    f = _csv(ws_dir, body="a,b\n1,2\n")
    scripted = [
        script(
            tool_calls=[
                script_tool_call("write_file", {"path": str(f), "content": "a,b\n9,9\n"})
            ]
        ),
        script(text="已更新。"),
    ]
    sid = create_session(scripted, workspace=str(ws_dir))

    with ws_conv(sid) as conv:
        conv.send(
            {
                "type": "invoke",
                "message": "改一下这个表",
                "files": [{"name": "data.csv", "path": str(f)}],
            }
        )
        events = conv.recv_until("message.end", "error")

    inv = [e for e in events if e["event"] == "preview.invalidate"]
    assert inv, f"no preview.invalidate in {[e['event'] for e in events]}"
    assert all(e["reason"].startswith("tool:") for e in inv)
    assert f.read_text(encoding="utf-8") == "a,b\n9,9\n"


def test_preview_fetch_clears_stale(client, create_session, ws_dir):
    sid = create_session(script(text="ok"), workspace=str(ws_dir))
    f = _csv(ws_dir)
    up = _upload(client, sid, "data.csv", f.read_bytes())
    fid = up["id"]
    # simulate the watcher marking it stale after an out-of-band change
    from ginno_runtime.files import get_registry

    reg = get_registry("default")
    reg.mark_stale(fid, True)
    f.write_text("a,b\n5,6\n", encoding="utf-8")
    pv = client.get(f"/api/files/{fid}/preview").json()
    assert pv["ok"] is True
    assert pv["file"]["stale"] is False  # cleared by the fetch
