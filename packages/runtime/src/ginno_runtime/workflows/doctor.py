"""Static dataflow lint for workflow DSLs (master-plan §4.2).

No LLM calls — pure structural analysis, millisecond-fast. Reused from three
places: the ``GET /api/workflows/{id}/doctor`` endpoint, the summarize
draft-retry hint, and the propose_edit validation. Each finding is a dict:
``{"rule", "node_id", "message"}``.

The flagship rule is ``loop.over.no_source`` — it catches the exact failure
mode of the 2026-08 stock-workflow incident (a loop iterating a context key
that no upstream step ever declares) *before* the run starts.
"""

from __future__ import annotations

import re

from .dsl import normalize_dsl

_CTX_REF = re.compile(r"\{\{\s*context\.([a-zA-Z0-9_]+)\s*\}\}")
_PATH_HINT = re.compile(r"/Users/|/home/|/tmp/|\.md\b|\.json\b")


def run_doctor(dsl: dict) -> dict:
    """Return ``{"errors": [...], "warnings": [...]}`` for a DSL."""
    d = normalize_dsl(dsl or {})
    errors: list[dict] = []
    warnings: list[dict] = []
    nodes = d.get("nodes") or []
    context_initial = (d.get("context") or {}).get("initial") or {}

    # Provenance: which context keys have a declared producer.
    writes_sources: dict[str, str] = {}  # key -> node id (or "__initial__")
    for k in context_initial:
        writes_sources[k] = "__initial__"
    for n in nodes:
        if not isinstance(n, dict):
            continue
        for k in (n.get("writes") or {}).keys():
            writes_sources.setdefault(k, n.get("id") or "?")

    # loop body id -> its `as` var (in scope inside the body's goal).
    loop_as_of_body: dict[str, str] = {}
    for n in nodes:
        if isinstance(n, dict) and n.get("type") == "loop" and n.get("body"):
            loop_as_of_body[n["body"]] = n.get("as") or "item"

    consumed: set[str] = set()

    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = n.get("id") or ""
        nt = n.get("type")
        goal = n.get("goal") or n.get("title") or ""

        # Reserved suffix (the compiler synthesizes <id>__extract nodes).
        if nt != "extract" and nid.endswith("__extract"):
            errors.append({
                "rule": "node_id.reserved_suffix", "node_id": nid,
                "message": f"节点 id '{nid}' 不得以 __extract 结尾（引擎保留后缀）",
            })

        # loop.over must have a declared source.
        if nt == "loop":
            over = n.get("over") or ""
            m = re.match(r"^\s*context\.([a-zA-Z0-9_]+)\s*$", str(over))
            if m:
                key = m.group(1)
                consumed.add(key)
                if key not in writes_sources:
                    errors.append({
                        "rule": "loop.over.no_source", "node_id": nid,
                        "message": (
                            f"loop '{nid}' over=context.{key} 无上游 writes/initial 来源"
                            f"（'{key}' 从未被任何节点声明产出）"
                        ),
                    })

        # goal {{context.X}} references must resolve.
        in_loop_body_as = loop_as_of_body.get(nid)
        for m in _CTX_REF.finditer(goal):
            key = m.group(1)
            consumed.add(key)
            if key not in writes_sources and key != in_loop_body_as:
                errors.append({
                    "rule": "goal.context_ref.no_source", "node_id": nid,
                    "message": (
                        f"节点 '{nid}' 的 goal 引用 {{{{context.{key}}}}} "
                        f"但该 key 无 writes/initial 来源"
                    ),
                })

        # Path literals in goals should live in context.initial (warn).
        if _PATH_HINT.search(goal):
            has_path_ctx = any(
                isinstance(v, str) and ("/" in v)
                for v in context_initial.values()
            )
            if not has_path_ctx:
                warnings.append({
                    "rule": "goal.path_literal", "node_id": nid,
                    "message": (
                        f"节点 '{nid}' 的 goal 含路径字面量，建议放入 "
                        f"context.initial 并作为受控路径声明"
                    ),
                })

    # writes keys never consumed downstream (warn).
    for key, src in writes_sources.items():
        if src == "__initial__":
            continue
        if key not in consumed:
            warnings.append({
                "rule": "writes.unused", "node_id": src,
                "message": f"节点 '{src}' 声明写入 '{key}' 但下游未消费",
            })

    return {"errors": errors, "warnings": warnings}
