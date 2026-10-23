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

import json
import re

from .. import agents as agents_reg
from ..lang import t
from .dsl import normalize_dsl

_CTX_REF = re.compile(r"\{\{\s*context\.([a-zA-Z0-9_]+)\s*\}\}")
_TEMPLATE_REF = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")
_PATH_HINT = re.compile(r"/Users/|/home/|/tmp/|\.md\b|\.json\b")


def run_doctor(
    dsl: dict, runs: list[dict] | None = None, version: int | None = None
) -> dict:
    """Return ``{"errors": [...], "warnings": [...]}`` for a DSL.

    ``runs`` (newest first) turns on the runtime checks: the static pass alone
    cannot see that an entry's return shape disagrees with its declared
    ``writes`` — the 2026-10-10 ``wiki-compile`` run died exactly there while
    ``doctor`` reported a clean bill of health. ``version`` keeps that evidence
    scoped to the definition being inspected: a failure from an older DSL
    version says nothing about the current one.
    """
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

        # Any ``{{key...}}`` template reference counts as consuming ``key``.
        # Without this, ``{{plan.files}}`` / ``report.broken`` read as unused
        # writes and the doctor floods a healthy workflow with warnings
        # (wiki-compile was reported clean-then-unused at the same time).
        for m in _TEMPLATE_REF.finditer(
            json.dumps(
                {k: n.get(k) for k in ("goal", "title", "args", "over") if n.get(k)},
                ensure_ascii=False,
            )
        ):
            if m.group(1) != "context":
                consumed.add(m.group(1))

        # Reserved suffix (the compiler synthesizes <id>__extract nodes).
        if nt != "extract" and nid.endswith("__extract"):
            errors.append({
                "rule": "node_id.reserved_suffix", "node_id": nid,
                "message": f"Node id '{nid}' must not end with __extract (reserved engine suffix)",
            })

        # Referenced agent must exist. The engine falls back at runtime since
        # 2026-08-10 (LLM-drafted DSLs invent role names) — doctor surfaces the
        # drafting mistake early instead of letting it hide in a warning event.
        ag = n.get("agent")
        if ag and agents_reg.get_agent(ag) is None:
            warnings.append({
                "rule": "agent.not_found", "node_id": nid,
                "message": (
                    t(f"Node '{nid}' references agent '{ag}' which does not exist; "
                      f"falls back to the default agent at runtime",
                      f"节点 '{nid}' 引用的 agent '{ag}' 不存在，"
                      f"运行时将回退到默认 agent")
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
                            "message": f"parallel loop '{nid}' body must declare writes",
                        })
                    else:
                        for k, v in bw.items():
                            if not isinstance(v, dict) or v.get("type") != "array":
                                errors.append({
                                    "rule": "loop.parallel.body_writes_not_array",
                                    "node_id": nid,
                                    "message": (
                                        t(f"parallel loop '{nid}' body writes key '{k}' "
                                          'must be {"type":"array"} (one element appended per item)',
                                          f"parallel loop '{nid}' 的 body writes 键 '{k}' "
                                          '必须为 {"type":"array"}（每 item 追加一元素）')
                                    ),
                                })
                    if body.get("type") not in ("step", "agent"):
                        errors.append({
                            "rule": "loop.parallel.body_type", "node_id": nid,
                            "message": f"parallel loop '{nid}' body must be a step/agent node",
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
                            t(f"loop '{nid}' over=context.{key} has no upstream writes/initial source "
                              f"('{key}' is never produced by any node)",
                              f"loop '{nid}' over=context.{key} 无上游 writes/initial 来源"
                              f"（'{key}' 从未被任何节点声明产出）")
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
                        t(f"Node '{nid}' goal references {{{{context.{key}}}}} "
                          f"but that key has no writes/initial source",
                          f"节点 '{nid}' 的 goal 引用 {{{{context.{key}}}}} "
                          f"但该 key 无 writes/initial 来源")
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
                        t(f"Node '{nid}' goal contains a path literal; consider moving it into "
                          f"context.initial as a declared controlled path",
                          f"节点 '{nid}' 的 goal 含路径字面量，建议放入 "
                          f"context.initial 并作为受控路径声明")
                    ),
                })

    # writes keys never consumed downstream (warn).
    for key, src in writes_sources.items():
        if src == "__initial__":
            continue
        if key not in consumed:
            warnings.append({
                "rule": "writes.unused", "node_id": src,
                "message": f"Node '{src}' declares write '{key}' but no downstream node consumes it",
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
                    t(f"Node '{f}' has {c} explicit out-edges; the engine only wires the first one "
                      "(parallel branches not implemented; extra edges were previously dropped silently)",
                      f"节点 '{f}' 有 {c} 条显式出边，引擎只接第一条"
                      "（并行分支未实现，多余边曾被静默丢弃）")
                ),
            }
            (errors if strict_multi else warnings).append(finding)

    _check_recent_runs(d, runs or [], errors, warnings, version)

    return {"errors": errors, "warnings": warnings}


_WRITES_SCHEMA_ERR = re.compile(
    r"python entry '([^']+)' output violates writes schema: (.+)$"
)


def _check_recent_runs(
    d: dict,
    runs: list[dict],
    errors: list[dict],
    warnings: list[dict],
    version: int | None = None,
) -> None:
    """Runtime evidence: a green static lint is not proof the workflow runs.

    Two rules (WF-02):
    * the newest run failed → error, quoting its node and message; a python
      node's writes-schema failure is reported against that node's entry so
      the fix location is obvious;
    * a recent failure rate ≥ 50% over at least two terminal runs → warning.
    """
    terminal = [
        r for r in runs if r.get("status") in ("done", "failed", "cancelled", "interrupted")
    ]
    if version is not None:
        # Only runs of the definition under inspection; legacy records without
        # the field are kept rather than silently ignored.
        terminal = [r for r in terminal if r.get("dsl_version") in (None, version)]
    if not terminal:
        return
    latest = terminal[0]
    if latest.get("status") == "failed":
        detail = latest.get("error_detail") or {}
        node_id = str(detail.get("node_id") or "")
        msg = str(latest.get("error") or "run failed")
        match = _WRITES_SCHEMA_ERR.search(msg)
        if match:
            entry = match.group(1)
            target = next(
                (
                    n
                    for n in d.get("nodes") or []
                    if isinstance(n, dict) and n.get("entry") == entry
                ),
                None,
            )
            errors.append({
                "rule": "python.writes_runtime_mismatch",
                "node_id": (target or {}).get("id") or node_id,
                "message": t(
                    f"Python entry '{entry}' returned a shape that violates its writes "
                    f"declaration in the latest run: {match.group(2)}",
                    f"最近一次运行中，python entry '{entry}' 的返回值不满足 writes 声明："
                    f"{match.group(2)}",
                ),
            })
        else:
            errors.append({
                "rule": "runs.latest_failed",
                "node_id": node_id,
                "message": t(
                    f"The latest run failed at node '{node_id or '?'}': {msg}",
                    f"最近一次运行在节点 '{node_id or '?'}' 失败：{msg}",
                ),
            })

    window = terminal[:5]
    failures = sum(1 for r in window if r.get("status") == "failed")
    if len(window) >= 2 and failures / len(window) >= 0.5:
        warnings.append({
            "rule": "runs.failure_rate",
            "node_id": "",
            "message": t(
                f"{failures} of the last {len(window)} runs failed",
                f"最近 {len(window)} 次运行中有 {failures} 次失败",
            ),
        })
