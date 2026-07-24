"""Sandboxed expression evaluation for the workflow DSL (design §3, Q3).

Two surfaces:
* ``render(template, context)`` — ``{{path}}`` substitution into step goals.
* ``eval_expr(expr, context)`` — safe boolean/value evaluation for branch
  ``when`` and loop ``over`` (loop-over-list is P3; here we evaluate conditions).

Safety: expressions are parsed with ``ast`` and walked; only a whitelist of node
types is honored (literals, names resolved against ``context``, dotted access
into context, a small set of builtins, comparisons/booleans/unary ops). No
attribute access beyond context dicts, no calls beyond the whitelist, no imports
or exec — so untrusted DSL expressions cannot escape the sandbox.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Any

_SAFE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_SAFE_CMPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _builtin(name: str):
    return {
        "len": len,
        "min": min,
        "max": max,
        "sum": sum,
        "abs": abs,
        "bool": bool,
        "int": int,
        "str": str,
        "any": any,
        "all": all,
    }.get(name)


def _resolve(name: str, parts: list[str], context: dict) -> Any:
    if name == "context":
        val: Any = context
    elif name in context:
        val = context[name]
    else:
        raise ValueError(f"unknown name '{name}'")
    for p in parts:
        if p.startswith("__") and p.endswith("__"):
            raise ValueError(f"dunder access '{p}' not allowed")
        if isinstance(val, dict):
            val = val.get(p)
        else:
            raise ValueError(f"cannot access '{p}' on non-mapping")
    return val


def _eval(node: ast.AST, context: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _resolve(node.id, [], context)
    if isinstance(node, ast.Attribute):
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            raise ValueError("unsupported attribute expression")
        return _resolve(cur.id, list(reversed(parts)), context)
    if isinstance(node, ast.Subscript):
        base = _eval(node.value, context)
        sl = node.slice
        key = _eval(sl.value, context) if isinstance(sl, ast.Index) else _eval(sl, context)  # type: ignore[attr-defined]
        return base[key]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, context) for e in node.elts]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise ValueError("unsupported unary op")
    if isinstance(node, ast.BinOp):
        op = _SAFE_BINOPS.get(type(node.op))
        if not op:
            raise ValueError("unsupported binary op")
        return op(_eval(node.left, context), _eval(node.right, context))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            r = True
            for v in node.values:
                r = _eval(v, context)
                if not r:
                    return r
            return r
        if isinstance(node.op, ast.Or):
            r = False
            for v in node.values:
                r = _eval(v, context)
                if r:
                    return r
            return r
    if isinstance(node, ast.Compare):
        left = _eval(node.left, context)
        for op, comp in zip(node.ops, node.comparators):
            fn = _SAFE_CMPS.get(type(op))
            if not fn:
                raise ValueError("unsupported comparison")
            right = _eval(comp, context)
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, context) if _eval(node.test, context) else _eval(node.orelse, context)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only simple function calls allowed")
        fn = _builtin(node.func.id)
        if fn is None:
            raise ValueError(f"function '{node.func.id}' not allowed")
        return fn(*[_eval(a, context) for a in node.args])
    raise ValueError(f"unsupported expression: {type(node).__name__}")


# Belt-and-suspenders: the walker is already a tight sandbox, but DSL
# expressions can originate from an LLM (`propose_edit`), so cap size/complexity
# to rule out a stack-blowing pathological expression (recursion depth in _eval
# is bounded by the AST node count).
_EXPR_CHAR_CAP = 4000
_EXPR_NODE_CAP = 300


def eval_expr(expr: str, context: dict) -> Any:
    """Evaluate a sandboxed expression against ``context`` (the WorkflowContext)."""
    src = (expr or "").strip()
    if len(src) > _EXPR_CHAR_CAP:
        raise ValueError("expression too long")
    tree = ast.parse(src, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > _EXPR_NODE_CAP:
        raise ValueError("expression too complex")
    return _eval(tree, context or {})


_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}")


def render(template: str, context: dict) -> str:
    """Substitute ``{{expr}}`` placeholders; missing/empty values render as ''."""

    def repl(m: re.Match) -> str:
        try:
            v = eval_expr(m.group(1), context)
        except Exception:
            return ""
        return "" if v is None else str(v)

    return _TEMPLATE_RE.sub(repl, template or "")


def eval_branch(node: dict, context: dict) -> str | None:
    """Return the target node id for a branch node: first matching case, else default."""
    for case in node.get("cases") or []:
        try:
            if eval_expr(case.get("when", ""), context):
                return case.get("then")
        except Exception:
            continue
    return node.get("default")
