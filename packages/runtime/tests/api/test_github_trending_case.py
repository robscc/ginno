"""Real-case e2e (design A): GitHub Trending → 选 repo → 学习 → 分析报告.

Strict three-step loop, all through real endpoints:
  1. 聊天完成一遍流程      (WS chat session, scripted agent)
  2. 总结 session 成 workflow (summarize-from-session -> create v1)
  3. 调试 workflow 到稳定   (run#1 发现 loop.over 取错键 -> PUT 修 DSL v2 -> run#2 稳定 done)
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


def _v1_dsl() -> dict:
    return {
        "name": "Trending Repo 分析",
        "entry": "fetch",
        "nodes": [
            {"id": "fetch", "type": "step", "agent": "dev", "goal": "拉取 GitHub Trending 列表"},
            {"id": "loop", "type": "loop", "over": "context.repos", "as": "repo", "body": "analyze", "max_iters": 5},
            {"id": "analyze", "type": "step", "agent": "dev", "goal": "分析 {{repo}} 并写分析报告"},
            {
                "id": "gate",
                "type": "branch",
                "cases": [
                    {"when": "len(context.reports) > 0", "then": "notify", "transform": {"expr": {"n": "len(context.reports)"}}}
                ],
                "default": "done",
            },
            {"id": "notify", "type": "llm", "prompt": "汇总 {{n}} 份报告", "output": "summary"},
            {"id": "done", "type": "pass"},
        ],
        "edges": [{"from": "fetch", "to": "loop"}, {"from": "loop", "to": "gate"}],
    }


def test_chat_then_summarize_then_debug_to_stable(client, create_session, ws_conv, patch_build_model):
    # ---- step 1: chat completes the flow ---- #
    chat = ScriptedChatModel(
        scripts=[
            script(text="今日 GitHub Trending: langgraph(★12k), marimo(★8k), reflex(★6k)。"),
            script(text="已选 langgraph：阅读 README/核心 state-graph 与 checkpointer 模块。"),
            script(text="分析报告(langgraph)：图状态机+可恢复检查点是其核心；适合智能体编排。"),
        ]
    )
    session_id = create_session(chat)
    with ws_conv(session_id) as conv:
        conv.invoke("看看今天 github trending 有什么好玩的 repo")
        conv.recv_until("message.end")
        conv.invoke("选 langgraph，学习一下它的核心模块")
        conv.recv_until("message.end")
        conv.invoke("给它写一份分析报告")
        conv.recv_until("message.end")

    # ---- step 2: summarize the session into a workflow ---- #
    synth = ScriptedChatModel(scripts=[script(text=json.dumps(_v1_dsl(), ensure_ascii=False))])
    patch_build_model(synth)
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": session_id})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body
    dsl_v1 = body["dsl"]
    cw = client.post("/api/workflows", json={"name": dsl_v1.get("name"), "dsl": dsl_v1})
    assert cw.status_code == 200
    wf_id = cw.json()["workflow"]["id"]

    # ---- step 3: debug to stable ---- #
    # run#1: fetch 实际写的是 repositories，而 v1 loop.over=context.repos -> 0 迭代（问题暴露）
    run1_model = ScriptedChatModel(scripts=[script(text='t\nWRITE_JSON {"repositories": ["langgraph", "marimo"]}'), script(text="x")])
    patch_build_model(run1_model)
    r1 = client.post("/api/workflow_runs", json={"workflow_id": wf_id})
    run1_id = r1.json()["run"]["id"]
    aw1 = client.post(f"/api/workflow_runs/{run1_id}/_await").json()
    ev1 = client.get(f"/api/workflow_runs/{run1_id}/events").json()["events"]
    assert aw1["run"]["status"] == "done"
    assert sum(1 for e in ev1 if e["kind"] == "loop_iter") == 0  # 取错键 -> 没跑分析

    # debug: 修 DSL v2，loop.over 指向真实键 repositories
    dsl_v2 = json.loads(json.dumps(_v1_dsl()))
    for n in dsl_v2["nodes"]:
        if n["id"] == "loop":
            n["over"] = "context.repositories"
    pu = client.put(f"/api/workflows/{wf_id}", json={"dsl": dsl_v2})
    assert pu.status_code == 200
    assert pu.json()["workflow"]["version"] == 2

    # run#2: stable
    run2_model = ScriptedChatModel(
        scripts=[
            script(text='t\nWRITE_JSON {"repositories": ["langgraph", "marimo"]}'),
            script(text='a\nWRITE_JSON {"reports": ["langgraph 报告"]}'),
            script(text='a\nWRITE_JSON {"reports": ["marimo 报告"]}'),
            script(text="汇总完成"),
        ]
    )
    patch_build_model(run2_model)
    r2 = client.post("/api/workflow_runs", json={"workflow_id": wf_id})
    run2_id = r2.json()["run"]["id"]
    aw2 = client.post(f"/api/workflow_runs/{run2_id}/_await").json()
    ev2 = client.get(f"/api/workflow_runs/{run2_id}/events").json()["events"]
    assert aw2["run"]["status"] == "done", aw2
    assert sum(1 for e in ev2 if e["kind"] == "loop_iter") == 2  # 每个 repo 都分析了
    enters2 = [e["node_id"] for e in ev2 if e["kind"] == "node_enter"]
    assert "analyze" in enters2 and "notify" in enters2
    written2 = {k for e in ev2 if e["kind"] == "context_write" for k in e["keys"]}
    assert {"repositories", "reports", "summary"} <= written2
