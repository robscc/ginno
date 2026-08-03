"""E2E: the closed loop INSIDE a session — 总结 / 唤起 / 修改 (design A).

1. 总结: chat a flow, then summarize-from-session -> create workflow v1.
2. 唤起: trigger a run bound to the session; the session WS receives run.bind +
   run.status(done) and the run record is present_in that session.
3. 修改: a workflow-dev session proposes a DSL edit (interrupt -> version.propose),
   the human applies it in-session, and the definition advances to v2.
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime import server
from ginno_runtime.testing.fake_model import script, script_tool_call
from ginno_runtime.workflows import store as wf_store

pytestmark = pytest.mark.e2e


def _patch(model):
    server.build_model = lambda *a, **k: model


def _v1_dsl() -> dict:
    return {
        "name": "SessionLoop",
        "entry": "fetch",
        "nodes": [
            {"id": "fetch", "type": "step", "agent": "dev", "goal": "拉取 trending"},
            {"id": "report", "type": "step", "agent": "dev", "goal": "写分析报告"},
        ],
        "edges": [{"from": "fetch", "to": "report"}],
    }


def test_session_summarize_invoke_modify(client, create_session, ws_conv):
    # ---------- 1) 总结: chat the flow ---------- #
    from ginno_runtime.testing.fake_model import ScriptedChatModel

    _patch(ScriptedChatModel(scripts=[script(text="trending: langgraph"), script(text="报告: 图状态机")]))
    session_id = create_session(ScriptedChatModel(scripts=[script(text="trending: langgraph"), script(text="报告: 图状态机")]))
    with ws_conv(session_id) as conv:
        conv.invoke("看看 trending")
        conv.recv_until("message.end")
        conv.invoke("写报告")
        conv.recv_until("message.end")

    _patch(ScriptedChatModel(scripts=[script(text=json.dumps(_v1_dsl(), ensure_ascii=False))]))
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": session_id})
    assert r.status_code == 200 and r.json()["ok"] is True
    dsl_v1 = r.json()["dsl"]
    wf_id = client.post("/api/workflows", json={"name": dsl_v1["name"], "dsl": dsl_v1}).json()["workflow"]["id"]

    # ---------- 2) 唤起: run bound to the session ---------- #
    _patch(
        ScriptedChatModel(
            scripts=[script(text='f\nWRITE_JSON {"x": 1}'), script(text='r\nWRITE_JSON {"y": 2}')]
        )
    )
    with ws_conv(session_id) as conv:
        rr = client.post("/api/workflow_runs", json={"workflow_id": wf_id, "session_id": session_id})
        run_id = rr.json()["run"]["id"]
        saw_bind = False
        status = None
        for _ in range(60):
            ev = conv.recv()
            if ev.get("event") == "run.bind":
                saw_bind = True
            if ev.get("event") == "run.status":
                status = ev.get("status")
                if status in ("done", "failed", "cancelled"):
                    break
        assert saw_bind, "session should receive run.bind for the invoked run"
        assert status == "done"
    bound = wf_store.get_run(run_id)
    assert bound["present_in_session_id"] == session_id

    # ---------- 3) 修改: workflow-dev proposes, human applies in-session ---------- #
    dsl_v2 = json.loads(json.dumps(_v1_dsl()))
    for n in dsl_v2["nodes"]:
        if n["id"] == "report":
            n["goal"] = "写更详细的分析报告（含架构）"
    dev_model = ScriptedChatModel(
        scripts=[
            script(text="", tool_calls=[script_tool_call("workflow_propose_edit", {"workflow_id": wf_id, "new_dsl_json": json.dumps(dsl_v2, ensure_ascii=False), "rationale": "更详细"})]),
            script(text="已应用。"),
        ]
    )
    dev_session = create_session(dev_model, agent_id="workflow-dev")
    with ws_conv(dev_session) as conv:
        conv.invoke("把 report 步骤改详细")
        evs = conv.recv_until("version.propose")
        assert any(e.get("event") == "version.propose" for e in evs)
        conv.respond_permission("allow")
        conv.recv_until("message.end")
    assert wf_store.get_def(wf_id)["version"] == 2
