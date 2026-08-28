"""Round-3 node system: registry + pluggable typed nodes."""

from __future__ import annotations

import textwrap

import pytest

from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows.nodes import BaseNode, known_types, load_plugin_modules, register_node
from ginno_runtime.workflows.nodes.builtin import AgentNode
from ginno_runtime.workflows.nodes.registry import get_node

pytestmark = pytest.mark.unit


def test_builtin_types_registered_with_aliases():
    types = known_types()
    for t in ("agent", "branch", "loop", "llm", "pass", "human"):
        assert t in types
    # step is an alias of agent; noop of pass
    assert get_node("step") is AgentNode
    assert get_node("agent") is AgentNode
    assert get_node("noop") is get_node("pass")


def test_unknown_type_is_none():
    assert get_node("definitely_not_a_node") is None


def test_register_custom_node_and_validate_accepts():
    @register_node
    class Ping(BaseNode):
        type = "t_ping"

        @staticmethod
        async def execute(node, cctx, state, config, eff):
            return {"events": [], "__output__": {}}

    assert get_node("t_ping") is Ping
    d = {
        "name": "w",
        "entry": "p",
        "nodes": [{"id": "p", "type": "t_ping"}],
        "edges": [],
    }
    assert wf_dsl.validate_dsl(wf_dsl.normalize_dsl(d)) == []


def test_validate_rejects_truly_unknown_type():
    d = {"name": "w", "entry": "x", "nodes": [{"id": "x", "type": "nope_type"}], "edges": []}
    assert any("unknown type" in e for e in wf_dsl.validate_dsl(wf_dsl.normalize_dsl(d)))


def test_load_plugin_modules_registers_from_file(tmp_path):
    plug = tmp_path / "myplug.py"
    plug.write_text(
        textwrap.dedent(
            """
            from ginno_runtime.workflows.nodes import BaseNode, register_node

            @register_node
            class PlugNode(BaseNode):
                type = "t_plug"
                params_schema = {"type": "object", "required": ["url"]}

                @staticmethod
                async def execute(node, cctx, state, config, eff):
                    return {"events": [], "__output__": {}}
            """
        ),
        encoding="utf-8",
    )
    loaded = load_plugin_modules([str(plug)])
    assert loaded == [str(plug)]
    cls = get_node("t_plug")
    assert cls is not None
    # param validation now enforced for plugin type
    errs = cls.validate_params({"id": "n", "type": "t_plug"})
    assert any("url" in e for e in errs)
