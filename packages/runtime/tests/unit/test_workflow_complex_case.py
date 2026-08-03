"""Round-3 complex end-to-end case: supervisor coerce + loop chaining + branch
case-transform + llm/pass nodes. Validates the fixes for loop "done/next" chaining
and branch transform placement discovered by simulating a full flow."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows import engine
from ginno_runtime.workflows.nodes import BaseNode, register_node

pytestmark = pytest.mark.unit


@register_node
class CxPrep(BaseNode):
    type = "t_cx_prep"
    inputs_schema = {
        "type": "object",
        "required": ["repo"],
        "properties": {"repo": {"type": "string", "default": "robscc/ginno"}},
    }

    @staticmethod
    async def execute(node, cctx, state, config, eff):
        ctx = dict(state.get("context") or {})
        return {"context": {**ctx, "repo": eff.get("repo")}, "events": [], "__output__": eff}


def _dsl():
    return {
        "name": "complex",
        "entry": "prep",
        "nodes": [
            {"id": "prep", "type": "t_cx_prep"},
            {"id": "fetch", "type": "step", "agent": "dev", "goal": "list prs"},
            {"id": "loop", "type": "loop", "over": "context.prs", "as": "pr", "body": "review", "max_iters": 5},
            {"id": "review", "type": "step", "agent": "dev", "goal": "review {{pr}}"},
            {
                "id": "gate",
                "type": "branch",
                "cases": [
                    {"when": "len(context.prs) > 0", "then": "notify", "transform": {"expr": {"n": "len(context.prs)"}}}
                ],
                "default": "done",
            },
            {"id": "notify", "type": "llm", "prompt": "summarize {{n}}", "output": "summary"},
            {"id": "done", "type": "pass"},
        ],
        "edges": [
            {"from": "prep", "to": "fetch"},
            {"from": "fetch", "to": "loop"},
            {"from": "loop", "to": "gate"},
        ],
    }


@pytest.mark.asyncio
async def test_complex_flow_chains_loop_branch_and_supervisor():
    assert wf_dsl.validate_dsl(wf_dsl.normalize_dsl(_dsl())) == []
    model = ScriptedChatModel(
        scripts=[
            script(text='f\nWRITE_JSON {"prs": ["a", "b"]}'),
            script(text='r1\nWRITE_JSON {"seen": "a"}'),
            script(text='r2\nWRITE_JSON {"seen": "b"}'),
            script(text="notified"),
        ]
    )
    events = []
    async for ev in engine.run_workflow(_dsl(), run_id="cx", model=model, tools=[], project_slug="unit-complex"):
        events.append(ev)

    kinds = [e["kind"] for e in events]
    # supervisor coerced the missing 'repo' input on prep
    assert any(e["kind"] == "supervisor_intervene" and e["action"] == "coerce" for e in events)
    # loop chained to the branch and the branch routed to notify (len>0)
    enters = [e["node_id"] for e in events if e["kind"] == "node_enter"]
    assert enters[:3] == ["fetch", "loop", "review"]
    assert "notify" in enters
    # two loop iterations
    assert sum(1 for e in events if e["kind"] == "loop_iter") == 2
    # context writes across nodes
    written = {k for e in events if e["kind"] == "context_write" for k in e["keys"]}
    assert {"prs", "seen", "summary"} <= written
    assert kinds[-1] == "done"
