"""Edge parameter adaptation (transform) between nodes (design A · round 3).

An edge may carry ``transform`` describing how the source node's typed output is
adapted into the target node's typed input. Supported keys:

* ``map``      — ``{"target_key": "src.path.or[0]"}`` copy fields from the source output.
* ``expr``     — ``{"target_key": "<sandboxed expr>"}`` evaluate against context+output.
* ``pick``     — list of source-output keys to include verbatim.
* ``defaults`` — ``{key: value}`` applied when absent.
* ``fn``       — name of a registered custom transform ``(src, context) -> dict``.

Default (no transform): the target input is the context shallow-merged with the
source output, so downstream nodes always see upstream results. Custom transforms
are registered with ``@register_transform`` (decoupled extension point).
"""

from __future__ import annotations

from typing import Any, Callable

from .. import expr as wf_expr

_TRANSFORMS: dict[str, Callable[[dict, dict], dict]] = {}


def register_transform(name: str, fn: Callable[[dict, dict], dict] | None = None) -> Callable:
    """Register a custom transform; usable directly or as ``@register_transform(name)``."""
    if fn is None:
        def deco(f: Callable[[dict, dict], dict]) -> Callable:
            _TRANSFORMS[name] = f
            return f

        return deco
    _TRANSFORMS[name] = fn
    return fn


def get_transform(name: str) -> Callable[[dict, dict], dict] | None:
    return _TRANSFORMS.get(name)


def _get_path(src: Any, path: str) -> Any:
    """Dotted/indexed path lookup into dicts/lists, e.g. ``prs[0].repo``."""
    cur = src
    token = ""
    i = 0
    path = path or ""
    while i < len(path):
        c = path[i]
        if c == ".":
            cur = _step(cur, token)
            token = ""
        elif c == "[":
            cur = _step(cur, token)
            token = ""
            j = path.index("]", i)
            idx = path[i + 1 : j]
            try:
                cur = cur[int(idx)] if isinstance(cur, list) else cur.get(idx) if isinstance(cur, dict) else None
            except Exception:
                return None
            i = j
        else:
            token += c
        i += 1
    if token:
        cur = _step(cur, token)
    return cur


def _step(cur: Any, key: str) -> Any:
    if not key:
        return cur
    if isinstance(cur, dict):
        return cur.get(key)
    return None


def apply_transform(transform: Any, src_output: dict, context: dict) -> dict:
    """Adapt ``src_output`` into the downstream node's input."""
    src = src_output or {}
    result: dict = {**context, **src}  # default: shallow merge
    if not transform or not isinstance(transform, dict):
        return result

    if "defaults" in transform and isinstance(transform["defaults"], dict):
        for k, v in transform["defaults"].items():
            result.setdefault(k, v)
    if "pick" in transform and isinstance(transform["pick"], list):
        picked = {k: src.get(k) for k in transform["pick"]}
        result.update(picked)
    if "map" in transform and isinstance(transform["map"], dict):
        for k, path in transform["map"].items():
            result[k] = _get_path(src, path) if isinstance(path, str) else path
    if "expr" in transform and isinstance(transform["expr"], dict):
        env = {**context, **src}
        for k, e in transform["expr"].items():
            try:
                result[k] = wf_expr.eval_expr(e, env)
            except Exception:
                result[k] = None
    if "fn" in transform and isinstance(transform["fn"], str):
        fn = get_transform(transform["fn"])
        if fn:
            try:
                out = fn(src, context)
                if isinstance(out, dict):
                    result.update(out)
            except Exception:
                pass
    return result
