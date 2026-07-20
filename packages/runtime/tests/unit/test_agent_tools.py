"""Unit tests for the agent-facing tool wrappers (todo/workflow/render/artifact)."""

from __future__ import annotations

import pytest

from ginno_runtime.tools import artifact_tools, render_tools, todo_tools, workflow_tools

pytestmark = pytest.mark.unit


# ------------------------------ todo ------------------------------ #
def test_todo_tool_create_and_list(isolated_home):
    out = todo_tools.todo_create.invoke({"title": "Task A", "priority": "high"})
    assert "created" in out and "Task A" in out
    listing = todo_tools.todo_list.invoke({})
    assert "Task A" in listing


def test_todo_list_empty(isolated_home):
    assert todo_tools.todo_list.invoke({}) == "(empty)"


def test_todo_done_and_delete(isolated_home):
    todo_tools.todo_create.invoke({"title": "T"})
    item_id = todo_tools.todo_list.invoke({}).split("]")[0].lstrip("[")
    assert "done" in todo_tools.todo_done.invoke({"todo_id": item_id})
    assert "deleted" in todo_tools.todo_delete.invoke({"todo_id": item_id})


def test_todo_create_invalid_priority_defaults_medium(isolated_home):
    todo_tools.todo_create.invoke({"title": "T", "priority": "urgent"})
    assert "(medium)" in todo_tools.todo_list.invoke({})


# ---------------------------- workflow ---------------------------- #
def test_workflow_create_and_list(isolated_home):
    out = workflow_tools.workflow_create.invoke(
        {"name": "WF", "description": "d", "steps_json": '[{"title": "a"}, {"title": "b"}]'}
    )
    assert "created workflow" in out
    assert "WF" in workflow_tools.workflow_list.invoke({})


def test_workflow_create_bad_json(isolated_home):
    out = workflow_tools.workflow_create.invoke({"name": "WF", "description": "d", "steps_json": "not json"})
    assert "not valid JSON" in out


def test_workflow_run_populates_cache(isolated_home):
    workflow_tools.workflow_create.invoke(
        {"name": "WF", "description": "d", "steps_json": '[{"title": "a"}]'}
    )
    out = workflow_tools.workflow_run.invoke({"name": "WF"})
    assert "started run_id=" in out
    assert len(workflow_tools.RUN_CACHE) == 1


def test_workflow_run_unknown(isolated_home):
    out = workflow_tools.workflow_run.invoke({"name": "ghost"})
    assert "not found" in out


def test_workflow_step_advances(isolated_home):
    workflow_tools.workflow_create.invoke(
        {"name": "WF", "description": "d", "steps_json": '[{"title": "a"}]'}
    )
    run_msg = workflow_tools.workflow_run.invoke({"name": "WF"})
    run_id = run_msg.split("run_id=")[1].split(" ")[0]
    step_id = run_msg.split("[")[1].split("]")[0]
    out = workflow_tools.workflow_step.invoke({"run_id": run_id, "step_id": step_id, "status": "done"})
    assert "1/1 steps complete" in out


# ----------------------- structured output ----------------------- #
def test_render_widget_returns_summary(isolated_home):
    out = render_tools.render_widget.invoke({"kind": "stat_list", "data": {"title": "x"}, "summary": "done"})
    assert out == "done"


def test_attach_ref_returns_label(isolated_home):
    out = render_tools.attach_ref.invoke({"kind": "file", "name": "notes.md"})
    assert "file" in out and "notes.md" in out


def test_artifact_register_returns_confirmation(isolated_home):
    out = artifact_tools.artifact_register.invoke({"kind": "doc", "name": "spec.md"})
    assert "doc" in out and "spec.md" in out


def test_tool_name_sets():
    assert todo_tools.TODO_TOOL_NAMES == {
        "todo_list", "todo_create", "todo_update", "todo_done", "todo_delete"
    }
    assert render_tools.RENDER_TOOL_NAMES == {"render_widget", "attach_ref"}
