"""Unit tests for the sandboxed DSL expression evaluator (P2, no io)."""

from __future__ import annotations

import pytest

from ginno_runtime.workflows import expr


def test_literals_and_arithmetic():
    assert expr.eval_expr("1 + 2 * 3", {}) == 7
    assert expr.eval_expr("'a' + 'b'", {}) == "ab"


def test_context_lookup_and_dot_access():
    ctx = {"repo": "x", "prs": [1, 2, 3]}
    assert expr.eval_expr("context.repo", ctx) == "x"
    assert expr.eval_expr("len(context.prs)", ctx) == 3


def test_comparisons_and_booleans():
    ctx = {"n": 2}
    assert expr.eval_expr("context.n > 1 and context.n < 5", ctx) is True
    assert expr.eval_expr("context.n == 2 or context.n == 3", ctx) is True
    assert expr.eval_expr("not context.n == 0", ctx) is True


def test_in_operator():
    assert expr.eval_expr("'a' in context.tags", {"tags": ["a", "b"]}) is True


def test_builtin_whitelist_and_rejection():
    assert expr.eval_expr("max(context.nums)", {"nums": [1, 4, 2]}) == 4
    with pytest.raises(ValueError):
        expr.eval_expr("open('x')", {})  # not in whitelist
    with pytest.raises(ValueError):
        expr.eval_expr("context.__class__", {"__class__": 1})  # dunder attr blocked


def test_unsupported_node_rejected():
    with pytest.raises(ValueError):
        expr.eval_expr("[x for x in context.a]", {"a": [1, 2]})  # comprehension blocked


def test_render_substitutes_and_swallows_missing():
    out = expr.render("repo={{context.repo}} n={{context.missing}}!", {"repo": "ginno"})
    assert out == "repo=ginno n=!"


def test_render_partial_keeps_unresolved_placeholders():
    """render_partial fills what it can and leaves the rest as {{…}} (display at
    run-creation time must not blank runtime-only placeholders)."""
    tpl = "平台 {{provider}}，id={{ext_id}}，来源={{upstream.summary}}"
    out = expr.render_partial(tpl, {"provider": "dingtalk"})
    assert out == "平台 dingtalk，id={{ext_id}}，来源={{upstream.summary}}"


def test_render_partial_empty_string_substitutes_none_kept():
    # explicit empty value is a real value -> substituted; None -> kept as-is
    assert expr.render_partial("mcp=[{{mcp}}]", {"mcp": ""}) == "mcp=[]"
    assert expr.render_partial("mcp=[{{mcp}}]", {"mcp": None}) == "mcp=[{{mcp}}]"


def test_eval_branch_first_match_then_default():
    node = {
        "cases": [
            {"when": "context.n > 10", "then": "big"},
            {"when": "context.n > 0", "then": "small"},
        ],
        "default": "none",
    }
    assert expr.eval_branch(node, {"n": 5}) == "small"
    assert expr.eval_branch(node, {"n": 0}) == "none"
    assert expr.eval_branch(node, {"n": 99}) == "big"
