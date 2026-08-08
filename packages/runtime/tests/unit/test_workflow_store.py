"""Unit tests for the workflow run store: delete_run + terminal-status guard."""

from __future__ import annotations

import pytest

from ginno_runtime.workflows import events as wf_events
from ginno_runtime.workflows import store

pytestmark = pytest.mark.unit


def _wf():
    return {
        "name": "w",
        "dsl": {
            "entry": "a",
            "nodes": [{"id": "a", "type": "step", "goal": "do a", "agent": "dev"}],
            "edges": [],
        },
    }


def test_delete_run_removes_json_and_events(isolated_home):
    wf = store.create_def(_wf())
    run = store.create_run(wf)
    rid = run["id"]
    wf_events.append_event(rid, "node_enter", node_id="a")
    assert store.get_run(rid) is not None

    assert store.delete_run(rid) is True
    assert store.get_run(rid) is None
    assert wf_events.read_events(rid) == []
    # idempotent: nothing left to remove
    assert store.delete_run(rid) is False


def test_update_step_does_not_clobber_terminal_status(isolated_home):
    wf = store.create_def(_wf())
    run = store.create_run(wf)
    rid = run["id"]
    step_id = run["steps"][0]["id"]

    # a failed run must stay failed even if a late step update arrives
    store._write_json(store._run_path(rid), {**store.get_run(rid), "status": "failed"})
    store.update_step(rid, step_id, "done")
    assert store.get_run(rid)["status"] == "failed"

    # same for interrupted / cancelled
    for terminal in ("interrupted", "cancelled"):
        store._write_json(store._run_path(rid), {**store.get_run(rid), "status": terminal})
        store.update_step(rid, step_id, "done")
        assert store.get_run(rid)["status"] == terminal


def test_update_step_completes_running_run(isolated_home):
    wf = store.create_def(_wf())
    run = store.create_run(wf)
    rid = run["id"]
    step_id = run["steps"][0]["id"]

    store.update_step(rid, step_id, "done")
    got = store.get_run(rid)
    assert got["status"] == "done"
    assert got["finished"] is not None


def _templated_wf():
    # Mirrors the seeded todo-push shape: a single agent node whose goal is a
    # {{template}} resolved per run from context_override.
    return {
        "name": "tpl",
        "dsl": {
            "entry": "push",
            "nodes": [
                {
                    "id": "push",
                    "type": "agent",
                    "agent": "dev",
                    "goal": (
                        "把外部 TODO 平台 {{provider}} 上 id={{ext_id}} 的待办「{{title}}」"
                        "标记为已完成（mcp={{mcp}}）。来源 {{upstream.summary}}。"
                    ),
                }
            ],
            "edges": [],
        },
    }


def test_create_run_renders_step_title_from_context_override(isolated_home):
    """Step titles in the persisted run are filled with the trigger's real values,
    not left as raw {{placeholders}} (the Workflow-panel display fix)."""
    wf = store.create_def(_templated_wf())
    run = store.create_run(
        wf,
        context_override={"provider": "dingtalk", "ext_id": "123", "title": "写周报", "mcp": "dt"},
    )
    title = run["steps"][0]["title"]
    assert "dingtalk" in title and "123" in title and "写周报" in title and "mcp=dt" in title
    assert "{{provider}}" not in title and "{{ext_id}}" not in title and "{{title}}" not in title


def test_create_run_keeps_unresolvable_placeholders(isolated_home):
    """Placeholders that only resolve at runtime (e.g. a previous step's output)
    must stay visible — not be blanked the way render() does for the agent goal."""
    wf = store.create_def(_templated_wf())
    run = store.create_run(wf, context_override={"provider": "dingtalk"})
    title = run["steps"][0]["title"]
    # known vars filled…
    assert "dingtalk" in title
    # …unknown vars (ext_id/title/mcp/upstream.summary) stay as placeholders
    assert "{{ext_id}}" in title and "{{title}}" in title and "{{mcp}}" in title
    assert "{{upstream.summary}}" in title


def test_create_run_renders_from_dsl_initial_context(isolated_home):
    """context.initial from the DSL is also a render source (override wins)."""
    wfdef = _templated_wf()
    wfdef["dsl"]["context"] = {"initial": {"provider": "jira", "ext_id": "9"}}
    wf = store.create_def(wfdef)
    run = store.create_run(wf, context_override={"ext_id": "42"})
    title = run["steps"][0]["title"]
    assert "jira" in title  # from context.initial
    assert "id=42" in title  # override beats initial


def test_create_run_prefers_explicit_title_over_goal(isolated_home):
    """When a node has both a short `title` and a verbose `goal`, the run step
    shows the title (rendered), not the full goal wall-of-text."""
    wf = store.create_def(
        {
            "name": "titled",
            "dsl": {
                "entry": "n",
                "nodes": [
                    {
                        "id": "n",
                        "type": "agent",
                        "agent": "dev",
                        "title": "同步 {{provider}}",
                        "goal": "一段很长很长的、带 {{provider}} 的执行指令……",
                    }
                ],
                "edges": [],
            },
        }
    )
    run = store.create_run(wf, context_override={"provider": "dingtalk"})
    assert run["steps"][0]["title"] == "同步 dingtalk"


def test_seeded_todo_sync_titles_render_concise(isolated_home):
    """The built-in todo-push/pull runs show a short, filled-in label in the run
    panel (their verbose agent goal stays in the definition, not the run card)."""
    store.ensure_seeded()

    push = store.get_def("todo-push")
    assert push is not None
    run = store.create_run(
        push,
        context_override={"provider": "dingtalk", "ext_id": "1", "title": "写周报", "skill": "dws", "mcp": ""},
    )
    assert run["steps"][0]["title"] == "把待办「写周报」标记为完成（dingtalk）"

    pull = store.get_def("todo-pull")
    assert pull is not None
    run2 = store.create_run(pull, context_override={"provider": "dingtalk", "skill": "dws", "mcp": ""})
    assert run2["steps"][0]["title"] == "拉取 dingtalk 的未完成待办"

