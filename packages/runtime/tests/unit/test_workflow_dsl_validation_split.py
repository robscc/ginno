"""Ordered validation results captured from the original DSL validator."""

import pytest

from ginno_runtime import world_state
from ginno_runtime.workflows.dsl import validate_dsl


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dsl", "expected"),
    [
        pytest.param(
            {
                "entry": "missing",
                "nodes": [
                    {"id": "repeat", "type": "unknown", "writes": {}},
                    {"id": "repeat", "type": "subflow", "retry": 3},
                    {"type": "step"},
                    {"id": "human", "type": "human", "on_error": "stop"},
                    {"id": "e__extract", "type": "extract"},
                ],
                "edges": ["malformed", {"from": "other", "to": "missing"}],
                "context": {"schema": [], "initial": 5},
                "supervisor": {"enabled": True, "retry_limit": 0},
            },
            [
                "node 'repeat' unknown type 'unknown'",
                "node 'repeat' writes must be a non-empty object",
                "node 'repeat' type 'subflow' is not supported until v2",
                "node 'repeat' retry must be an object",
                "nodes[2] missing id",
                "node 'human' on_error is only supported on step/agent/llm/browser nodes",
                "node 'e__extract' type 'extract' is compiler-internal — declare `writes` on a step node instead",
                "node 'e__extract' id must not end with '__extract' (reserved)",
                "duplicate node id",
                "entry 'missing' is not a node id",
                "edges[0] must be an object",
                "edges[1].from 'other' unknown",
                "edges[1].to 'missing' unknown",
                "step 'None' needs a goal (or title)",
                "<root>: missing required 'source_node'",
                "<root>: missing required 'writes'",
                "context.schema must be an object",
                "context.initial must be an object",
                "supervisor.mode must be 'auto' or 'human' when enabled",
                "supervisor.retry_limit must be an integer >= 1",
            ],
            id="mixed-errors",
        ),
        pytest.param(
            {
                "entry": "branch",
                "nodes": [
                    {"id": "branch", "type": "branch", "cases": [{"then": "gone"}]},
                    {"id": "loop", "type": "loop", "body": "step", "parallel": True},
                    {"id": "step", "type": "agent", "writes": {"key": {"type": "string"}}},
                ],
                "edges": [
                    {"from": "branch", "to": "loop", "transform": {"fn": 7}},
                    {"from": "loop", "to": "step"},
                    {"from": "loop", "to": "branch"},
                    {"from": "loop", "to": "gone"},
                    {"from": "step", "to": "branch"},
                ],
            },
            [
                "edge from 'branch' not allowed (branch routes via cases/default; put a transform on a case)",
                "edges[0].transform.fn must be a string",
                "edge from 'loop' to its body not allowed (body returns to loop head)",
                "edges[3].to 'gone' unknown",
                "loop 'loop' may have at most one explicit out-edge (the done/next edge)",
                "edge from 'step' not allowed (loop body returns to loop head)",
                "branch 'branch' case[0] needs when+then",
                "branch 'branch' case[0].then 'gone' unknown",
                "loop 'loop' needs over",
                "loop 'loop' needs max_iters >= 1",
                "loop 'loop' is parallel: body writes key 'key' "
                'must be {"type":"array"} (one element per item)',
                "step 'step' needs a goal (or title)",
            ],
            id="routing-and-params",
        ),
        pytest.param(
            {
                "dsl_version": "1",
                "entry": "legacy",
                "unknown_workflow_field": True,
                "nodes": [
                    {"id": "legacy", "type": "step", "title": "Old title", "agent_id": "a"},
                    {"id": "wait", "type": "human", "unknown_node_field": 1},
                ],
            },
            [],
            id="legacy-and-unknown-fields",
        ),
    ],
)
def test_validate_dsl_error_order(dsl, expected, monkeypatch):
    monkeypatch.setattr(world_state, "context_settings", lambda: {})
    assert validate_dsl(dsl) == expected


@pytest.mark.unit
@pytest.mark.parametrize("strict", [True, False])
def test_validate_dsl_multi_edge_setting(strict, monkeypatch):
    monkeypatch.setattr(
        world_state, "context_settings", lambda: {"workflow_strict_multi_edge": strict}
    )
    dsl = {
        "entry": "start",
        "nodes": [{"id": nid, "type": "step", "goal": nid} for nid in ("start", "a", "b")],
        "edges": [{"from": "start", "to": target} for target in ("a", "b")],
    }
    expected = [
        "node 'start' has 2 explicit out-edges; only linear chains are "
        "supported (parallel fan-out is not implemented for this node type)"
    ]
    assert validate_dsl(dsl) == (expected if strict else [])


@pytest.mark.unit
@pytest.mark.parametrize(
    "node",
    [{"id": [], "type": "step"}, {"id": "a", "type": []}, {"id": "a", "type": "loop", "body": []}],
)
def test_validate_dsl_unhashable_node_fields(node):
    with pytest.raises(TypeError, match="unhashable type: 'list'"):
        validate_dsl({"entry": "a", "nodes": [node]})
