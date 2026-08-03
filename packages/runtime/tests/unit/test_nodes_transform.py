"""Round-3 node system: edge transforms (parameter adaptation between nodes)."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import engine
from ginno_runtime.workflows.nodes import BaseNode, register_node
from ginno_runtime.workflows.nodes.transforms import apply_transform, register_transform

pytestmark = pytest.mark.unit


@register_node
class Echo(BaseNode):
    """Test node: emits its effective input as an 'echo' event + writes to context."""

    type = "t_echo"

    @staticmethod
    async def execute(node, cctx, state, config, eff):
        run_ctx = cctx["run_ctx"]
        ctx = dict(state.get("context") or {})
        run_ctx["events"].append({"run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "echo", "data": eff})
        return {"context": {**ctx, "got": eff}, "events": [], "__output__": eff}


def test_default_merge_context_and_output():
    out = apply_transform(None, {"a": 1}, {"b": 2})
    assert out["a"] == 1 and out["b"] == 2


def test_map_path_from_source_output():
    src = {"prs": [{"repo": "x"}, {"repo": "y"}]}
    out = apply_transform({"map": {"first": "prs[0].repo"}}, src, {})
    assert out["first"] == "x"


def test_expr_eval_against_context_plus_output():
    out = apply_transform({"expr": {"n": "len(items)"}}, {"items": [1, 2, 3]}, {})
    assert out["n"] == 3


def test_defaults_and_pick():
    out = apply_transform({"defaults": {"mode": "auto"}, "pick": ["a"]}, {"a": 1, "z": 9}, {})
    assert out["mode"] == "auto"
    assert out["a"] == 1 and "z" in out  # pick merges over the default merge


def test_custom_fn_transform():
    @register_transform("double")
    def _double(src, ctx):
        return {"doubled": src.get("v", 0) * 2}

    out = apply_transform({"fn": "double"}, {"v": 21}, {})
    assert out["doubled"] == 42


@pytest.mark.asyncio
async def test_engine_applies_edge_transform_to_downstream_input():
    d = {
        "name": "w",
        "entry": "a",
        "nodes": [
            {"id": "a", "type": "step", "agent": "dev", "goal": "produce"},
            {"id": "e", "type": "t_echo"},
        ],
        "edges": [{"from": "a", "to": "e", "transform": {"map": {"repo": "repo_name"}}}],
    }
    model = ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"repo_name":"robscc/ginno"}')])
    events = []
    async for ev in engine.run_workflow(d, run_id="rt", model=model, tools=[]):
        events.append(ev)
    echoes = [e for e in events if e["kind"] == "echo"]
    assert len(echoes) == 1
    # downstream input carries the transformed field pulled from upstream output
    assert echoes[0]["data"]["repo"] == "robscc/ginno"
    assert events[-1]["kind"] == "done"
