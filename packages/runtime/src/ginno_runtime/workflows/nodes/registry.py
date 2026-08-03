"""Node-type registry + plugin loading (design A · round 3).

Node types are decoupled from the core: a type is just a :class:`BaseNode`
subclass registered here. New types can be added by:

* importing a module that calls ``@register_node`` (e.g. ``load_plugin_modules``),
* exposing an entry-point in the ``ginno_runtime.workflow_nodes`` group, or
* dropping a module path into ``GINNO_NODE_PLUGINS`` (comma-separated).

``supervisor``/``compiler``/``dsl`` only ever talk to the registry, never to
concrete node classes — so the set of node types is open-ended.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from typing import Type

_REGISTRY: dict[str, Type] = {}
_ALIASES: dict[str, str] = {}
_loaded = False


def register_node(cls: Type) -> Type:
    """Class decorator: register a node type (and its aliases)."""
    if not getattr(cls, "type", ""):
        raise ValueError(f"node class {cls.__name__} must define 'type'")
    _REGISTRY[cls.type] = cls
    for a in getattr(cls, "aliases", ()) or ():
        _ALIASES[a] = cls.type
    return cls


def canonical(type_or_alias: str) -> str | None:
    if type_or_alias in _REGISTRY:
        return type_or_alias
    return _ALIASES.get(type_or_alias)


def get_node(type_or_alias: str) -> Type | None:
    c = canonical(type_or_alias)
    return _REGISTRY.get(c) if c else None


def get_or_raise(type_or_alias: str) -> Type:
    cls = get_node(type_or_alias)
    if cls is None:
        raise ValueError(f"unknown workflow node type '{type_or_alias}'")
    return cls


def known_types() -> list[str]:
    return sorted(_REGISTRY)


def load_plugin_modules(paths: list[str]) -> list[str]:
    """Import modules by file path; each is expected to self-register node types."""
    loaded = []
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        name = f"ginno_node_plugin_{os.path.splitext(os.path.basename(p))[0]}"
        spec = importlib.util.spec_from_file_location(name, p)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # registers via @register_node on import
        loaded.append(p)
    return loaded


def load_plugins() -> None:
    """Idempotent plugin discovery: entry-points + GINNO_NODE_PLUGINS."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    # 1) entry points (packaged plugins)
    try:
        eps = importlib.metadata.entry_points()
        group = eps.select(group="ginno_runtime.workflow_nodes") if hasattr(eps, "select") else eps.get("ginno_runtime.workflow_nodes", [])
        for ep in group:
            try:
                ep.load()
            except Exception:
                continue
    except Exception:
        pass
    # 2) env-var module paths (loose plugins)
    env = os.environ.get("GINNO_NODE_PLUGINS", "")
    if env:
        load_plugin_modules([p for p in env.split(",") if p.strip()])
