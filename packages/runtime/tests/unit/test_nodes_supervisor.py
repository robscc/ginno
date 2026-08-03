"""Round-3 node system: supervisor intervenes when node params/inputs fail validation."""

from __future__ import annotations

import pytest

from ginno_runtime.workflows import engine
from ginno_runtime.workflows.nodes import BaseNode, register_node

pytestmark = pytest.mark.unit


@register_node
class NeedsRepoDefault(BaseNode):
    """Input requires 'repo' but schema supplies a default -> supervisor coerces."""

    type = "t_need_repo_default"
    inputs_schema = {"type": "object", "required": ["repo"], "properties": {"repo": {"type": "string", "default": "default/repo"}}}

    @staticmethod
    async def execute(node, cctx, state, config, eff):
        run_ctx = cctx["run_ctx"]
        run_ctx["events"].append({"run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "echo", "data": eff})
        return {"events": [], "__output__": eff}


@register_node
class NeedsRepoStrict(BaseNode):
    """Input requires 'repo' with no default and no coercion path -> supervisor aborts."""

    type = "t_need_repo_strict"
    inputs_schema = {"type": "object", "required": ["repo"], "properties": {"repo": {"type": "string"}}}

    @staticmethod
    async def execute(node, cctx, state, config, eff):
        return {"events": [], "__output__": eff}


@pytest.mark.asyncio
async def test_supervisor_coerces_missing_input_from_default():
    d = {"name": "w", "entry": "n", "nodes": [{"id": "n", "type": "t_need_repo_default"}], "edges": []}
    events = []
    async for ev in engine.run_workflow(d, run_id="rs1", model=None, tools=[]):
        events.append(ev)
    kinds = [e["kind"] for e in events]
    assert "supervisor_intervene" in kinds
    sup = [e for e in events if e["kind"] == "supervisor_intervene"][0]
    assert sup["action"] == "coerce"
    # node ran with the coerced input
    echo = [e for e in events if e["kind"] == "echo"][0]
    assert echo["data"]["repo"] == "default/repo"
    assert kinds[-1] == "done"


@pytest.mark.asyncio
async def test_supervisor_aborts_when_unrecoverable():
    d = {"name": "w", "entry": "n", "nodes": [{"id": "n", "type": "t_need_repo_strict"}], "edges": []}
    events = []
    async for ev in engine.run_workflow(d, run_id="rs2", model=None, tools=[]):
        events.append(ev)
    kinds = [e["kind"] for e in events]
    assert "supervisor_intervene" in kinds
    sup = [e for e in events if e["kind"] == "supervisor_intervene"][0]
    assert sup["action"] == "abort"
    # engine surfaces the abort as an error event, not a false 'done'
    assert any(e["kind"] == "error" for e in events)
    assert kinds[-1] != "done"
