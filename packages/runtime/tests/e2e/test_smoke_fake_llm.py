"""全流程确定性冒烟(odw-comparison P2-④,``make smoke`` 的测试体)。

不依赖网络与真实 provider:server 装配 → 建会话 → 一轮对话 → 一个
workflow run 走完。CI 与发版前的最短「整机能转」证据链。"""

from __future__ import annotations

import time

import pytest

from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def test_full_flow_smoke(client, isolated_home, create_session, ws_conv):
    # create_session 吃模型脚本列表;多给两个脚本(workflow 的 llm 节点与
    # 转录线共用 fork,脚本耗尽返回空文本但不失败——给足避免误判)。
    model = script(
        text="smoke reply " + "x" * 100,
        usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    )
    sid = create_session([model] * 4)

    # 1) server 装配:无副作用路由存活(/api/mods 会 ensure_started 拉起真实
    # broker,不适合冒烟环境——mods 面由 test_mods_* 覆盖)。
    assert client.get("/api/badge").json()["ok"] is True
    assert client.get("/api/schedule").json()["enabled"] in (True, False)
    assert client.get("/api/usage/overview?days=1").json()["totals"] is not None

    # 2) 会话 + 一轮对话(WS invoke)
    with ws_conv(sid) as conv:
        conv.invoke("say smoke")
        events = conv.recv_until("message.end", "error")
        kinds = [e.get("event") for e in events]
        assert "turn.start" in kinds and "error" not in kinds

    # 3) workflow 定义 + run(引擎全链:编译→执行→落盘)
    wf = client.post(
        "/api/workflows",
        json={"name": "smoke-wf", "dsl": {
            "nodes": [{"id": "n1", "type": "llm", "prompt": "hi"}],
            "edges": [], "entry": "n1",
        }},
    ).json()
    assert wf.get("ok") is True, wf
    wf_id = wf["workflow"]["id"]
    run = client.post("/api/workflow_runs", json={"workflow_id": wf_id}).json()
    assert run["ok"] is True
    run_id = run["run"]["id"]

    deadline = time.time() + 30
    status = "running"
    while time.time() < deadline:
        row = client.get(f"/api/workflow_runs/{run_id}").json()
        status = (row.get("run") or {}).get("status") or "?"
        if status in ("done", "failed", "error", "cancelled"):
            break
        time.sleep(0.2)
    assert status == "done", f"workflow run ended as {status}"

    # 4) run 行 + events 都落盘且可读(inspector 面)
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    assert any(e["kind"] == "node_exit" for e in evs)
