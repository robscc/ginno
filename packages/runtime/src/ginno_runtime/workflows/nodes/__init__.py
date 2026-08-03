"""Workflow node-type system (design A · round 3).

Importing this package registers the built-in node types (see :mod:`builtin`) and
exposes the registry/transform/base primitives used by ``dsl``/``compiler`` and by
third-party plugins.

Add a new node type without touching core::

    from ginno_runtime.workflows.nodes import BaseNode, register_node

    @register_node
    class MyNode(BaseNode):
        type = "my_node"
        params_schema = {"type": "object", "required": ["url"]}
        @staticmethod
        async def execute(node, cctx, state, config, eff):
            ...  # return a state-update dict; set "__output__" for downstream edges
"""

from . import base, registry, transforms
from .base import BaseNode
from .registry import (
    get_node,
    get_or_raise,
    known_types,
    load_plugin_modules,
    load_plugins,
    register_node,
)

# Importing builtin triggers @register_node for the shipped node types.
from . import builtin  # noqa: F401,E402

__all__ = [
    "BaseNode",
    "register_node",
    "get_node",
    "get_or_raise",
    "known_types",
    "load_plugins",
    "load_plugin_modules",
    "base",
    "registry",
    "transforms",
    "builtin",
]
