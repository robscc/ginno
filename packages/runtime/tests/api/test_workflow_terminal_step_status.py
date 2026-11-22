"""R3 巡检 Q10：run 终态后步骤不许永久「运行中」+ 事件流 node_exit 协议。

三同根缺陷（同一族事件协议缺口）：
1. PassNode/HumanNode 不发射 node_exit —— run driver 的 step 终态回写挂在
   node_exit 上，全 pass/human 的 run 结束后这些步骤永远显示「运行中」；
2. 失败路径只发 error 事件，失败节点没有配对的 node_exit(failed) ——
   run JSON 靠 API 层 error 兜底才不失真，事件流本身不自洽；
3. HumanNode 的 interrupt 事件无条件 append —— resume 重放时节点从头再跑，
   interrupt 事件被第二次落盘（每次 resume 重复一条）。

driver 级验收（HTTP API + 真 LangGraph + FileCheckpointer，零 mock 模型）：
* 全 pass 的 run 直接 done，步骤全 done；
* pass→human→pass 的 run resume 后步骤全 done，无 running；
* 失败节点步骤终态 failed 且事件流含 node_exit(failed)；
* resume 后 interrupt 事件恰好一条（幂等）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _patch_model(monkeypatch, models: list):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model", lambda *a, **k: models.pop(0)
    )


class _OkModel:
    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, messages, *a, **k):
        return AIMessage(content="ok")


def _pass_only_wf():
    return {
        "name": "PassOnly",
        "dsl": {
            "entry": "p1",
            "nodes": [
                {"id": "p1", "type": "pass"},
                {"id": "p2", "type": "pass"},
            ],
            "edges": [{"from": "p1", "to": "p2"}],
        },
    }


def _pass_human_pass_wf():
    return {
        "name": "PassHumanPass",
        "dsl": {
            "entry": "p1",
            "nodes": [
                {"id": "p1", "type": "pass"},
                {"id": "h", "type": "human", "question": "approve?"},
                {"id": "p2", "type": "pass"},
            ],
            "edges": [
                {"from": "p1", "to": "h"},
                {"from": "h", "to": "p2"},
            ],
        },
    }


def _fail_mid_chain_wf():
    """n1(llm) -> n2(llm) -> n3(pass)：模型在第二次调用时抛错。"""
    return {
        "name": "FailMidChain",
        "dsl": {
            "entry": "n1",
            "nodes": [
                {"id": "n1", "type": "llm", "prompt": "first", "output": "a"},
                {"id": "n2", "type": "llm", "prompt": "second", "output": "b"},
                {"id": "n3", "type": "pass"},
            ],
            "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
        },
    }


class _FailOnSecondCallModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, messages, *a, **k):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="n1 done")
        raise RuntimeError("boom-n2")


def _step_statuses(run: dict) -> dict[str, str]:
    return {s["id"]: s["status"] for s in run.get("steps", [])}


def _events(client, run_id: str) -> list[dict]:
    return client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]


def test_all_pass_run_finishes_with_all_steps_done(client, monkeypatch):
    """Q10-① 原始复现：全 pass 的 run —— 旧代码里 pass 步骤永久「运行中」。"""
    _patch_model(monkeypatch, [_OkModel()])
    wf = store.create_def(_pass_only_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    assert _step_statuses(aw["run"]) == {"p1": "done", "p2": "done"}
    # 事件流自洽：每个 node_enter 都有配对 node_exit
    kinds = [(e["kind"], e.get("node_id"), e.get("status")) for e in _events(client, run_id)]
    assert [k for k in kinds if k[0] == "node_exit"] == [
        ("node_exit", "p1", "done"),
        ("node_exit", "p2", "done"),
    ], kinds


def test_pass_human_pass_steps_all_terminal_after_resume(client, monkeypatch):
    """pass→human→pass：resume 后（含 human 步骤自身）无一步停在「运行中」。"""
    _patch_model(monkeypatch, [_OkModel(), _OkModel()])
    wf = store.create_def(_pass_human_pass_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "paused", aw
    # 挂起语义：h 在等待（running），p1 已完成
    assert _step_statuses(aw["run"]) == {"p1": "done", "h": "running", "p2": "pending"}

    r = client.post(f"/api/workflow_runs/{run_id}/resume", json={"answer": "yes"})
    assert r.status_code == 200 and r.json()["status"] == "resuming", r.text
    aw2 = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw2["run"]["status"] == "done", aw2
    assert _step_statuses(aw2["run"]) == {"p1": "done", "h": "done", "p2": "done"}


def test_failed_node_step_terminal_and_node_exit_failed_event(client, monkeypatch):
    """Q10-②：失败节点的步骤终态 failed，事件流含配对 node_exit(failed)。"""
    _patch_model(monkeypatch, [_FailOnSecondCallModel()])
    wf = store.create_def(_fail_mid_chain_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "failed", aw
    statuses = _step_statuses(aw["run"])
    assert statuses == {"n1": "done", "n2": "failed", "n3": "pending"}, statuses
    assert "running" not in statuses.values()

    evs = _events(client, run_id)
    failed_exits = [e for e in evs if e["kind"] == "node_exit" and e.get("status") == "failed"]
    assert len(failed_exits) == 1 and failed_exits[0]["node_id"] == "n2", evs
    # 顺序契约：node_exit(failed) 必须在 error 之后（API 层先标 run failed，
    # 否则 update_step 会把 running run 重算成 done —— 见 engine._append_run_error）
    kinds = [e["kind"] for e in evs]
    fail_i = next(
        i for i, e in enumerate(evs)
        if e["kind"] == "node_exit" and e.get("status") == "failed"
    )
    assert kinds.index("error") < fail_i


def test_resume_is_idempotent_interrupt_event_not_duplicated(client, monkeypatch):
    """Q10-③：resume 重放不得把 interrupt 事件再落一条。"""
    _patch_model(monkeypatch, [_OkModel(), _OkModel()])
    wf = store.create_def(_pass_human_pass_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # paused
    before = [e for e in _events(client, run_id) if e["kind"] == "interrupt"]
    assert len(before) == 1, before

    r = client.post(f"/api/workflow_runs/{run_id}/resume", json={"answer": "yes"})
    assert r.status_code == 200, r.text
    client.post(f"/api/workflow_runs/{run_id}/_await")  # done

    evs = _events(client, run_id)
    # 幂等：resume 之后 interrupt 仍恰好一条（旧代码这里变两条）
    assert len([e for e in evs if e["kind"] == "interrupt"]) == 1, evs
    assert len([e for e in evs if e["kind"] == "resume"]) == 1, evs
    # human 步骤的配对终态事件也在
    h_exits = [e for e in evs if e["kind"] == "node_exit" and e.get("node_id") == "h"]
    assert len(h_exits) == 1 and h_exits[0].get("status") == "done", evs
