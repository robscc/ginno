"""Unit tests for the file-backed stores: todos, workflows, artifacts."""

from __future__ import annotations

import pytest

from ginno_runtime import artifacts as art_store
from ginno_runtime import workflows as wf_store
from ginno_runtime.todos import store as todo_store

pytestmark = pytest.mark.unit


# ----------------------------- todos ----------------------------- #
def test_todo_create_assigns_defaults(isolated_home):
    t = todo_store.create_todo({"title": "Write tests"})
    assert t["id"]
    assert t["title"] == "Write tests"
    assert t["priority"] == "medium"
    assert t["done"] is False
    assert t["completed_at"] is None


def test_todo_create_requires_title(isolated_home):
    with pytest.raises(ValueError):
        todo_store.create_todo({"title": "   "})


def test_todo_update_done_sets_completed_at(isolated_home):
    t = todo_store.create_todo({"title": "x"})
    updated = todo_store.update_todo(t["id"], {"done": True})
    assert updated["done"] is True
    assert updated["completed_at"] is not None
    # un-done clears it
    assert todo_store.update_todo(t["id"], {"done": False})["completed_at"] is None


def test_todo_update_unknown_returns_none(isolated_home):
    assert todo_store.update_todo("nope", {"done": True}) is None


def test_todo_delete(isolated_home):
    t = todo_store.create_todo({"title": "x"})
    assert todo_store.delete_todo(t["id"]) is True
    assert todo_store.delete_todo(t["id"]) is False
    assert todo_store.list_todos() == []


def test_todo_seed_when_empty(isolated_home):
    todo_store.ensure_seeded()
    assert len(todo_store.list_todos()) == 7
    # idempotent
    todo_store.ensure_seeded()
    assert len(todo_store.list_todos()) == 7


# --------------------------- workflows --------------------------- #
def test_workflow_def_crud(isolated_home):
    wf = wf_store.create_def(
        {"name": "Deploy", "description": "d", "steps": [{"title": "build"}, {"title": "ship"}]}
    )
    assert wf["id"]
    assert len(wf["steps"]) == 2
    assert wf_store.get_def(wf["id"])["name"] == "Deploy"
    assert any(d["id"] == wf["id"] for d in wf_store.list_defs())
    assert wf_store.delete_def(wf["id"]) is True
    assert wf_store.get_def(wf["id"]) is None


def test_workflow_run_step_rollup(isolated_home):
    wf = wf_store.create_def({"name": "W", "steps": [{"title": "a"}, {"title": "b"}]})
    run = wf_store.create_run(wf)
    assert run["status"] == "running"
    assert all(s["status"] == "pending" for s in run["steps"])
    step_ids = [s["id"] for s in run["steps"]]

    wf_store.update_step(run["id"], step_ids[0], "done", "ok")
    run = wf_store.get_run(run["id"])
    assert run["status"] == "running"  # one step still pending

    wf_store.update_step(run["id"], step_ids[1], "done", "ok")
    run = wf_store.get_run(run["id"])
    assert run["status"] == "done"  # all steps complete


def test_workflow_seed(isolated_home):
    wf_store.ensure_seeded()
    assert wf_store.get_def("pr-triage") is not None


# --------------------------- artifacts --------------------------- #
def test_artifact_add_and_dedup(isolated_home):
    a1 = art_store.add_artifact("default", "file", "notes.md", "/v/notes.md")
    a2 = art_store.add_artifact("default", "file", "notes.md", "/v/notes.md")
    assert a1["id"] == a2["id"]  # de-duped by (kind, ref)
    assert len(art_store.list_artifacts("default")) == 1


def test_artifact_list_sorted_desc(isolated_home):
    art_store.add_artifact("default", "file", "a.md", "a")
    art_store.add_artifact("default", "doc", "b.md", "b")
    items = art_store.list_artifacts("default")
    assert items[0]["created"] >= items[-1]["created"]
    assert len(items) == 2


def test_artifact_delete(isolated_home):
    a = art_store.add_artifact("default", "file", "notes.md", "/v/notes.md")
    b = art_store.add_artifact("default", "doc", "plan.md", "/v/plan.md")
    assert art_store.delete_artifact("default", a["id"]) is True
    # foolproof: re-deleting, empty ids, and unknown ids never touch anything
    assert art_store.delete_artifact("default", a["id"]) is False
    assert art_store.delete_artifact("default", "") is False
    assert art_store.delete_artifact("default", "nope") is False
    assert [it["id"] for it in art_store.list_artifacts("default")] == [b["id"]]
    assert art_store.delete_artifact("default", b["id"]) is True
    assert art_store.list_artifacts("default") == []


def test_artifact_delete_scoped_to_slug(isolated_home):
    a = art_store.add_artifact("default", "file", "x.md", "/v/x.md")
    assert art_store.delete_artifact("other", a["id"]) is False
    assert len(art_store.list_artifacts("default")) == 1


def test_artifact_get_and_update(isolated_home):
    a = art_store.add_artifact("default", "file", "report.xlsx", "/v/report.xlsx")
    assert art_store.get_artifact("default", a["id"])["name"] == "report.xlsx"
    assert art_store.get_artifact("default", "") is None
    assert art_store.get_artifact("default", "nope") is None

    # whitelist applies; system fields are immune
    upd = art_store.update_artifact(
        "default",
        a["id"],
        {"name": "Q3 Report", "schema": "[Sheet1] 3行×2列", "id": "hax", "created": 0},
    )
    assert upd["name"] == "Q3 Report"
    assert upd["schema"] == "[Sheet1] 3行×2列"
    assert upd["id"] == a["id"] and upd["created"] == a["created"]

    # blank name rejects the WHOLE patch (nothing half-applied)
    assert art_store.update_artifact("default", a["id"], {"name": "  ", "kind": "link"}) is None
    assert art_store.get_artifact("default", a["id"])["kind"] == "file"

    assert art_store.update_artifact("default", "nope", {"name": "x"}) is None
    # edits are persisted (file-backed, re-read from disk)
    assert art_store.get_artifact("default", a["id"])["name"] == "Q3 Report"


def test_artifact_list_session_filter(isolated_home):
    art_store.add_artifact("default", "file", "s1.xlsx", "/v/s1.xlsx", session_id="s1")
    art_store.add_artifact("default", "file", "s2.xlsx", "/v/s2.xlsx", session_id="s2")
    art_store.add_artifact("default", "link", "naked", "https://x")  # no session_id

    all_items = art_store.list_artifacts("default")
    assert len(all_items) == 3  # None → unscoped (back-compat)
    s1 = art_store.list_artifacts("default", session_id="s1")
    assert [a["name"] for a in s1] == ["s1.xlsx"]
    s2 = art_store.list_artifacts("default", session_id="s2")
    assert [a["name"] for a in s2] == ["s2.xlsx"]
    # the session_id=None row is invisible to any scoped query
    assert art_store.list_artifacts("default", session_id="s3") == []


def test_artifact_set_ref_in_place(isolated_home):
    a = art_store.add_artifact("default", "file", "r.csv", "/old/r.csv", session_id="s1")
    upd = art_store.set_ref("default", a["id"], "/new/r.csv")
    assert upd["ref"] == "/new/r.csv"
    assert art_store.get_artifact("default", a["id"])["ref"] == "/new/r.csv"
    # in-place: still exactly one record (no dedupe-key duplicate)
    assert len(art_store.list_artifacts("default")) == 1
    assert art_store.set_ref("default", "nope", "/x") is None
    assert art_store.set_ref("default", "", "/x") is None
