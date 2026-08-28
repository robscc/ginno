"""Stability plan P1d: the dev-agent ``workflow_dry_run`` tool + the
``ensure_workflow_dev_tools`` upgrade migration.

The endpoint/tool share ``workflows.dryrun.dry_run_dsl`` (endpoint behaviour is
pinned by tests/api/test_workflow_dry_run.py), so here we only cover the tool
surface: draft vs stored-DSL inputs, failure rendering, and the idempotent
tools_allow merge for installs seeded before P1d.
"""

from __future__ import annotations

import json
from pathlib import Path

from ginno_runtime import paths
from ginno_runtime.agents import ensure_workflow_dev_tools, get_agent
from ginno_runtime.tools.workflow_tools import workflow_dry_run

_GOOD_DSL = {
    "name": "PR Review",
    "entry": "s1",
    "nodes": [
        {"id": "s1", "type": "step", "agent": "research", "goal": "list PRs"},
        {"id": "s2", "type": "step", "agent": "dev", "goal": "review each"},
    ],
    "edges": [{"from": "s1", "to": "s2"}],
}


def test_tool_preflights_draft_dsl(isolated_home: Path):
    out = workflow_dry_run.invoke({"new_dsl_json": json.dumps(_GOOD_DSL)})
    assert out.startswith("ok: DSL compiles cleanly")
    assert "2 nodes" in out


def test_tool_reports_draft_errors(isolated_home: Path):
    bad = dict(_GOOD_DSL, entry="nope")
    out = workflow_dry_run.invoke({"new_dsl_json": json.dumps(bad)})
    assert out.startswith("failed:")
    assert "nope" in out


def test_tool_rejects_bad_json(isolated_home: Path):
    assert "not valid JSON" in workflow_dry_run.invoke({"new_dsl_json": "{oops"})
    assert "JSON object" in workflow_dry_run.invoke({"new_dsl_json": "[1,2]"})


def test_tool_requires_an_input(isolated_home: Path):
    out = workflow_dry_run.invoke({})
    assert out.startswith("error:") and "workflow_id" in out


def test_tool_preflights_stored_workflow(isolated_home: Path, monkeypatch):
    from ginno_runtime.tools import workflow_tools as wt

    monkeypatch.setattr(
        wt.wf_store, "get_def", lambda _id: {"id": _id, "dsl": _GOOD_DSL}
    )
    out = workflow_dry_run.invoke({"workflow_id": "wf-x"})
    assert out.startswith("ok: DSL compiles cleanly")

    monkeypatch.setattr(wt.wf_store, "get_def", lambda _id: None)
    assert workflow_dry_run.invoke({"workflow_id": "missing"}).startswith("error:")


# --------------------------------------------------------------------------- #
# Migration: pre-P1d installs gain workflow_dry_run in tools_allow
# --------------------------------------------------------------------------- #
def _seed_agent(allow: list[str]) -> None:
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    (paths.agents_dir() / "workflow-dev.json").write_text(
        json.dumps(
            {
                "id": "workflow-dev",
                "name": "Workflow Dev Agent",
                "icon": "workflow",
                "color": "violet",
                "system_prompt": "x",
                "provider": "custom",
                "tools_allow": allow,
            }
        ),
        encoding="utf-8",
    )


def test_migration_adds_dry_run_once(isolated_home: Path):
    _seed_agent(["workflow_propose_edit", "workflow_list"])
    ensure_workflow_dev_tools()
    assert get_agent("workflow-dev").tools_allow == [
        "workflow_propose_edit",
        "workflow_list",
        "workflow_dry_run",
    ]
    # Idempotent: a second pass must not duplicate.
    ensure_workflow_dev_tools()
    assert get_agent("workflow-dev").tools_allow == [
        "workflow_propose_edit",
        "workflow_list",
        "workflow_dry_run",
    ]


def test_migration_skips_wildcard_agents(isolated_home: Path):
    _seed_agent(["*"])
    ensure_workflow_dev_tools()
    assert get_agent("workflow-dev").tools_allow == ["*"]
