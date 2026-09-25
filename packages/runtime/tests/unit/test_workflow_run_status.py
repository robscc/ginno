"""workflow_run_status tool (the 2026-09-25 fix): agents must be able to see
LIVE run state. Born from a real incident — the workflow-dev agent answered
「都在 pending，去 UI 点」from the creation-time snapshot while the run was
already executing (the turn stream had adopted it), because its toolset had
no way to query run status at all."""

from __future__ import annotations

import pytest

from ginno_runtime.tools.workflow_tools import workflow_run_status
from ginno_runtime.workflows import events as wf_events
from ginno_runtime.workflows import store

pytestmark = pytest.mark.unit


def _wf():
    return store.create_def({
        "name": "StatusT",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "a"},
                {"id": "s2", "type": "llm", "prompt": "b"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        },
    })


def test_live_status_of_a_parked_run(isolated_home):
    paths = None  # noqa: F841 — isolation fixture alone is the point
    wf = _wf()
    run = store.create_run(wf)
    # simulate the engine having driven s1 and parked at a human-ish interrupt
    store.update_step(run["id"], "s1", "done")
    _r = store.get_run(run["id"])
    _r["status"] = "paused"
    _r["pending_interrupt"] = {
        "kind": "human",
        "node_id": "s2",
        "question": "补好凭证了吗？回复 ready 继续",
    }
    store._write_json(store._run_path(run["id"]), _r)
    wf_events.append_event(run["id"], "node_enter", node_id="s1")
    wf_events.append_event(run["id"], "node_exit", node_id="s1", status="done")
    wf_events.append_event(run["id"], "interrupt", node_id="s2", question="补好凭证了吗？")

    out = workflow_run_status.invoke({"run_id": run["id"]})
    assert f"run_id={run['id']}" in out  # stream adoption regex key
    assert "status=paused" in out
    assert "waiting (user must answer): kind=human node=s2" in out
    assert "补好凭证" in out
    assert "[s1] done" in out and "[s2] pending" in out
    assert "recent events:" in out and "node_exit" in out


def test_failed_run_shows_error(isolated_home):
    wf = _wf()
    run = store.create_run(wf)
    _r = store.get_run(run["id"])
    _r["status"] = "failed"
    _r["error"] = "RuntimeError: provider down"
    store._write_json(store._run_path(run["id"]), _r)
    out = workflow_run_status.invoke({"run_id": run["id"]})
    assert "status=failed" in out and "provider down" in out


def test_no_run_id_returns_the_newest_run(isolated_home):
    wf = _wf()
    a = store.create_run(wf)
    b = store.create_run(wf)
    out = workflow_run_status.invoke({})
    assert f"run_id={b['id']}" in out  # newest first
    assert a["id"] not in out.split("\n")[0]


def test_unknown_and_empty(isolated_home):
    assert "error: run not found" in workflow_run_status.invoke({"run_id": "zzz"})
    # no runs at all in this isolated home
    assert "error: run not found" in workflow_run_status.invoke({})


def test_glob_refuses_filesystem_root(isolated_home):
    """glob_files 从 "/" 起扫必须直接拒绝（2026-09-25：打包应用 cwd="/"，
    agent 的 **/gmail_dump/* 全盘遍历挂死 run）。"""
    from ginno_runtime.tools.builtin import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools(workspace="/")}
    out = tools["glob_files"].invoke({"pattern": "**/x/*"})
    assert "[error]" in out and "filesystem root" in out
    out2 = tools["grep_files"].invoke({"pattern": "x"})
    assert "[error]" in out2 and "filesystem root" in out2
