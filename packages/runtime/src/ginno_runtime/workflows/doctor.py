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

from .. import agents as agents_reg
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

        # Referenced agent must exist. The engine falls back at runtime since
        # 2026-08-10 (LLM-drafted DSLs invent role names) — doctor surfaces the
        # drafting mistake early instead of letting it hide in a warning event.
        ag = n.get("agent")
        if ag and agents_reg.get_agent(ag) is None:
            warnings.append({
                "rule": "agent.not_found", "node_id": nid,
                "message": (
                    f"节点 '{nid}' 引用的 agent '{ag}' 不存在，"
                    f"运行时将回退到默认 agent"
                ),
            })

        # loop.over must have a declared source.
        if nt == "loop":
            # Parallel loops (stability plan P3) tighten the body contract;
            # surface the same findings validate_dsl hard-rejects so drafts
            # see them in the panel before a run.
            if n.get("parallel"):
                body = next(
                    (x for x in nodes if isinstance(x, dict) and x.get("id") == n.get("body")),
                    None,
                )
                if isinstance(body, dict):
                    bw = body.get("writes")
                    if not bw or not isinstance(bw, dict):
                        errors.append({
                            "rule": "loop.parallel.body_writes_not_array", "node_id": nid,
                            "message": f"parallel loop '{nid}' 的 body 必须声明 writes",
                        })
                    else:
                        for k, v in bw.items():
                            if not isinstance(v, dict) or v.get("type") != "array":
                                errors.append({
                                    "rule": "loop.parallel.body_writes_not_array",
                                    "node_id": nid,
                                    "message": (
                                        f"parallel loop '{nid}' 的 body writes 键 '{k}' "
                                        '必须为 {"type":"array"}（每 item 追加一元素）'
                                    ),
                                })
                    if body.get("type") not in ("step", "agent"):
                        errors.append({
                            "rule": "loop.parallel.body_type", "node_id": nid,
                            "message": f"parallel loop '{nid}' 的 body 必须是 step/agent 节点",
                        })
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

    # Multi-out-edge graphs: the compiler only ever wired each node's FIRST
    # out-edge (pre-P2), so extra edges were silently dropped. Severity tracks
    # the same strict flag as validate_dsl — error by default (make the latent
    # mis-route visible), warning when the flag is rolled off.
    try:
        from .. import world_state as ws_mod

        strict_multi = bool(ws_mod.context_settings().get("workflow_strict_multi_edge", True))
    except Exception:
        strict_multi = True
    by_type = {n.get("id"): n.get("type") for n in nodes if isinstance(n, dict)}
    out_count: dict[str, int] = {}
    for e in d.get("edges") or []:
        if not isinstance(e, dict):
            continue
        f = e.get("from")
        if by_type.get(f) in ("branch", "loop"):
            continue  # branch out-edges forbidden; loop capped by validate
        out_count[f] = out_count.get(f, 0) + 1
    for f, c in out_count.items():
        if c > 1:
            finding = {
                "rule": "edges.multi_out.unsupported", "node_id": f,
                "message": (
                    f"节点 '{f}' 有 {c} 条显式出边，引擎只接第一条"
                    "（并行分支未实现，多余边曾被静默丢弃）"
                ),
            }
            (errors if strict_multi else warnings).append(finding)

    return {"errors": errors, "warnings": warnings}
