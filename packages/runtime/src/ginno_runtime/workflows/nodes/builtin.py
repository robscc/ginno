"""Built-in workflow node types (design A · round 3).

These are the *general-purpose* nodes shipped with Ginno; more can be added as
plugins without touching core (see :mod:`registry`). ``step`` is kept as an alias
of ``agent`` so existing DSLs and tests keep working.
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.prebuilt import ToolNode

from ... import agents as agents_reg
from ...graph import text_of_content, tool_allowed
from .. import expr as wf_expr
from . import agent_helpers as ah
from .base import BaseNode, llm_invoke_with_timeout
from .registry import register_node


def _usage_from_msg(msg) -> dict:
    """Extract token usage from a chat-model response (master-plan §4.5).

    Providers differ: OpenAI puts it under ``response_metadata.token_usage``
    (prompt/completion), Anthropic under ``response_metadata.usage``
    (input/output). Returns ``{}`` when nothing is available so callers can
    merge harmlessly.
    """
    meta = getattr(msg, "response_metadata", None) or {}
    tu = meta.get("token_usage") or {}
    if tu:
        return {
            "input_tokens": tu.get("prompt_tokens", 0),
            "output_tokens": tu.get("completion_tokens", 0),
        }
    u = meta.get("usage") or {}
    if u:
        return {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
        }
    return {}


async def _run_agent_turn(node, cctx, state, render_ctx, emit) -> tuple[str, dict, str | None]:
    """One autonomous goal→tools loop (extracted for the parallel gather
    adapter, stability plan P3; AgentNode.execute keeps its own sequential
    copy for now). Returns ``(result_text, usage, agent_warning)``; emits
    tool_call/tool_result via ``emit``. node_enter/exit and WRITE_JSON
    bookkeeping stay with the caller."""
    run_ctx = cctx["run_ctx"]
    model = cctx["model"]
    tools = cctx["tools"]
    node_id = node["id"]
    max_iters = int(node.get("max_tool_iters") or 8)
    agent, agent_warning = ah.resolve_agent(node.get("agent"))
    goal = wf_expr.render(node.get("goal") or node.get("title") or "", render_ctx)

    mcp_prefix = ""
    prov_id = str(render_ctx.get("provider") or "")
    if prov_id:
        from ...todos import providers as todo_providers

        _prov = todo_providers.get_todo_provider(prov_id)
        if _prov and _prov.get("mcp"):
            mcp_prefix = f"mcp_{_prov['mcp']}_"
    allowed = [
        t
        for t in tools
        if (tool_allowed(agent, t.name) or (mcp_prefix and t.name.startswith(mcp_prefix)))
        and not t.name.startswith("workflow_")
    ]
    bound = model.bind_tools(allowed) if allowed and hasattr(model, "bind_tools") else model
    tool_node = ToolNode(allowed, handle_tool_errors=True) if allowed else None

    sys_text = ah.build_system(goal, dict(state.get("context") or {}), agent)
    skill_names = [
        wf_expr.render(s, render_ctx)
        for s in (node.get("skills") or [])
        if isinstance(s, str) and s.strip()
    ]
    if skill_names:
        from ...skills.loader import SkillLoader

        loader = SkillLoader(project_slug="default")
        secs = []
        for nm in skill_names:
            sk = loader.get(nm)
            if sk and sk.body:
                secs.append(f'<skill name="{sk.name}">\n{sk.body.strip()}\n</skill>')
        if secs:
            sys_text += "\n\n## Injected skills\n" + "\n\n".join(secs)

    msgs = [SystemMessage(content=sys_text), HumanMessage(content=goal)]
    result_text = ""
    usage: dict = {"input_tokens": 0, "output_tokens": 0}
    for it in range(max_iters):
        if it:
            from .. import engine as wf_engine

            wf_engine.check_pause(run_ctx, node_id)
        resp = await llm_invoke_with_timeout(bound.ainvoke(msgs))
        msgs.append(resp)
        result_text = text_of_content(resp.content)
        u = ah.record_model_usage(resp, run_ctx.get("usage_attr")) or _usage_from_msg(resp)
        for k, v in u.items():
            usage[k] = usage.get(k, 0) + v
        calls = getattr(resp, "tool_calls", None) or []
        if not calls:
            break
        emit({
            "run_id": run_ctx["run_id"], "node_id": node_id, "kind": "tool_call",
            "calls": [{"name": c.get("name"), "args": c.get("args")} for c in calls],
        })
        if tool_node is None:
            break
        tres = await tool_node.ainvoke({"messages": [resp]})
        tmsgs = tres["messages"] if isinstance(tres, dict) else tres
        for tm in tmsgs:
            c = text_of_content(tm.content)
            emit({
                "run_id": run_ctx["run_id"], "node_id": node_id, "kind": "tool_result",
                "name": getattr(tm, "name", ""), "content": c[:2000],
            })
        msgs.extend(tmsgs)
    return result_text, usage, agent_warning


async def _execute_parallel_body(node, owner, cctx, state, eff) -> dict:
    """Gather mode for a parallel loop's body (stability plan P3).

    Runs every item's agent turn concurrently (bounded by the loop's
    max_concurrency), extracts per item (writes keys are validated as arrays;
    each item contributes one element, index-ordered), and writes the assembled
    arrays back to context in ONE state update — so the LastValue channels
    never see concurrent writers. One node_enter/node_exit pair wraps the
    whole batch, keeping the step-status machinery's one-step view intact;
    per-item progress rides loop_iter/loop_item_error events.

    Failure policy reuses the node's P2 fields per item: retry first, then
    on_error=continue records the index as null (loop_item_error + soft
    failure count) while stop re-raises and fails the run."""
    from langgraph.errors import GraphBubbleUp, GraphRecursionError

    from .. import dsl as wf_dsl
    from .. import supervisor as wf_sup
    from .extract import extract_from_text

    run_ctx = cctx["run_ctx"]
    node_id = node["id"]
    as_var = owner.get("as") or "item"
    items = (state.get("loop_vars") or {}).get(as_var)
    items = items if isinstance(items, list) else []
    _, max_conc = wf_dsl.loop_parallel_spec(owner)
    writes_schema = node.get("writes") or {}
    # Per-item schema = the array's items sub-schema (one element per item).
    per_item_schema = {
        k: (v.get("items") if isinstance(v, dict) and isinstance(v.get("items"), dict) else {})
        for k, v in writes_schema.items()
    }
    on_error = node.get("on_error") or "stop"
    spec = node.get("retry") or {}
    try:
        max_attempts = max(1, min(int(spec.get("max_attempts") or 1), 10))
    except (TypeError, ValueError):
        max_attempts = 1
    backoff = spec.get("backoff") or "fixed"
    try:
        backoff_ms = int(spec.get("backoff_ms") if spec.get("backoff_ms") is not None else 1000)
    except (TypeError, ValueError):
        backoff_ms = 1000

    context = dict(state.get("context") or {})
    events: list = []

    def emit(ev):
        ev.setdefault("ts", time.time())
        events.append(ev)
        run_ctx["events"].append(ev)

    emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter",
          "node_type": "step", "parallel": True, "of": len(items)})

    sem = asyncio.Semaphore(max(1, max_conc))
    texts: list[str] = [""] * len(items)
    values: dict[str, list] = {k: [None] * len(items) for k in writes_schema}
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    _CONTROL = (asyncio.CancelledError, GraphBubbleUp, GraphRecursionError, wf_sup.SupervisorAbort)

    async def _one(i: int, item) -> None:
        async with sem:
            from .. import engine as wf_engine

            wf_engine.check_pause(run_ctx, node_id)  # pause boundary per item
            # Per-item progress (same observable contract as sequential loops:
            # one loop_iter per iteration — here per item as it starts).
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "loop_iter",
                  "index": i, "of": len(items), "parallel": True})
            render_ctx = {**context, as_var: item, **(eff or {})}
            attempt = 1
            while True:
                try:
                    text, usage, _warn = await _run_agent_turn(node, cctx, state, render_ctx, emit)
                    vals, err = await extract_from_text(
                        text, per_item_schema, cctx["model"], run_ctx, f"{node_id}[{i}]",
                        fast_path=True, extract_model_name=node.get("extract_model"),
                    )
                    if vals is None:
                        raise RuntimeError(f"item {i} extraction failed: {err}")
                    break
                except _CONTROL:
                    raise
                except Exception as exc:
                    if attempt >= max_attempts:
                        raise
                    wait = backoff_ms if backoff != "exponential" else backoff_ms * (2 ** (attempt - 1))
                    wait = min(wait, 30_000)
                    emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                          "kind": "node_retry", "attempt": attempt,
                          "max_attempts": max_attempts, "item": i,
                          "error": f"{type(exc).__name__}: {exc}", "backoff_ms": wait})
                    await asyncio.sleep(wait / 1000.0)
                    attempt += 1
            for k in writes_schema:
                values[k][i] = vals.get(k)
            texts[i] = text
            for ku, vu in usage.items():
                usage_total[ku] = usage_total.get(ku, 0) + vu

    results = await asyncio.gather(
        *[_one(i, it) for i, it in enumerate(items)], return_exceptions=True
    )
    soft = 0
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            if isinstance(r, _CONTROL):
                raise r
            if on_error == "continue":
                soft += 1
                emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                      "kind": "loop_item_error", "index": i, "action": "continue",
                      "error": f"{type(r).__name__}: {r}"})
            else:
                raise r
    if soft:
        run_ctx["soft_failures"] = int(run_ctx.get("soft_failures") or 0) + soft

    meta = dict(state.get("context_meta") or {})
    new_context = dict(context)
    for k in writes_schema:
        new_context[k] = values[k]
        meta[k] = f"step:{node_id}"
    emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "context_write",
          "keys": list(writes_schema)})
    emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit",
          "status": "done", "usage": usage_total, "parallel": True})
    return {
        "context": new_context,
        "context_meta": meta,
        "results": {**state.get("results", {}), node_id: "\n\n".join(t for t in texts if t)},
        "events": events,
        "__output__": {k: values[k] for k in writes_schema},
    }


@register_node
class AgentNode(BaseNode):
    """General-purpose autonomous agent step: pursue a goal with tools, write context."""

    type = "agent"
    aliases = ("step",)
    params_schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "agent": {"type": "string"},
            # Optional skill names injected into the system prompt (chat-style
            # <skill> wrappers) — lets a step carry platform know-how (e.g. the
            # dws skill for todo-provider sync). Entries may be {{templates}}.
            "skills": {"type": "array", "items": {"type": "string"}},
        },
    }
    inputs_schema = {"type": "object"}
    outputs_schema = {"type": "object"}

    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        if node.get("goal") or node.get("title"):
            return []
        return [f"step '{node.get('id')}' needs a goal (or title)"]

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        # Parallel-loop body in gather mode (stability plan P3): the loop head
        # published the full item table into loop_vars and expects ONE batched
        # activation that writes index-ordered arrays back.
        from .. import dsl as wf_dsl

        owner = wf_dsl.parallel_loop_owner(cctx.get("dsl") or {}, node["id"])
        if owner is not None and (
            (state.get("loop_iters") or {}).get(owner["id"]) or {}
        ).get("parallel"):
            return await _execute_parallel_body(node, owner, cctx, state, eff)

        run_ctx = cctx["run_ctx"]
        model = cctx["model"]
        tools = cctx["tools"]
        node_id = node["id"]
        max_iters = int(node.get("max_tool_iters") or 8)
        # Fallback instead of fail: a DSL may reference an agent that doesn't
        # exist (LLM-drafted DSLs invent role names). The run continues with
        # a substituted persona and a warning event (2026-08-10 incident).
        agent, agent_warning = ah.resolve_agent(node.get("agent"))
        context = dict(state.get("context") or {})
        loop_vars = dict(state.get("loop_vars") or {})
        render_ctx = {**context, **loop_vars, **(eff or {})}
        goal = wf_expr.render(node.get("goal") or node.get("title") or "", render_ctx)
        events: list = []

        # Provider sync runs (todo-pull/push et al.): unlock the provider's
        # MCP server tools even when the forked agent's tools_allow doesn't
        # list them — the provider config (settings → todo_providers.mcp) is
        # the capability declaration for MCP-based platforms.
        mcp_prefix = ""
        prov_id = str(render_ctx.get("provider") or "")
        if prov_id:
            from ...todos import providers as todo_providers

            _prov = todo_providers.get_todo_provider(prov_id)
            if _prov and _prov.get("mcp"):
                mcp_prefix = f"mcp_{_prov['mcp']}_"
        allowed = [
            t
            for t in tools
            if (tool_allowed(agent, t.name) or (mcp_prefix and t.name.startswith(mcp_prefix)))
            and not t.name.startswith("workflow_")
        ]
        bound = model.bind_tools(allowed) if allowed and hasattr(model, "bind_tools") else model
        # handle_tool_errors=True: a raising tool must degrade to an error
        # ToolMessage the step can react to, never kill the whole workflow run
        # (same discipline as the main chat graph).
        tool_node = ToolNode(allowed, handle_tool_errors=True) if allowed else None

        sys_text = ah.build_system(goal, context, agent)
        # Configurable skill injection (todo-provider sync et al.): entries are
        # template-rendered so a single generic workflow can serve any provider
        # (the trigger passes the resolved skill via context_override).
        skill_names = [
            wf_expr.render(s, render_ctx)
            for s in (node.get("skills") or [])
            if isinstance(s, str) and s.strip()
        ]
        if skill_names:
            from ...skills.loader import SkillLoader

            loader = SkillLoader(project_slug="default")
            secs = []
            for nm in skill_names:
                sk = loader.get(nm)
                if sk and sk.body:
                    secs.append(f'<skill name="{sk.name}">\n{sk.body.strip()}\n</skill>')
            if secs:
                sys_text += "\n\n## Injected skills\n" + "\n\n".join(secs)

        # Incremental flush: every event lands in run_ctx["events"] as it is
        # produced (engine streams it → API persists + pushes it). If the step
        # raises mid-flight, its node_enter/tool_call/tool_result footprints are
        # already recorded — a batch flush at the end would lose them all.
        # ``events`` is still returned in the state update (LangGraph reducer).
        def emit(ev):
            ev.setdefault("ts", time.time())
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "step"})
        if agent_warning:
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "warning", "message": agent_warning})
        msgs = [SystemMessage(content=sys_text), HumanMessage(content=goal)]
        result_text = ""
        usage = {"input_tokens": 0, "output_tokens": 0}
        for it in range(max_iters):
            if it:
                # Manual-pause boundary (workflow-ux-redesign #14): between
                # tool iterations, so a long step can be paused without waiting
                # for it to finish. The checkpoint only commits per superstep,
                # so pausing here rewinds the WHOLE step — it re-executes from
                # scratch on resume (accepted semantics; duplicated events /
                # usage mirror the retry path).
                from .. import engine as wf_engine

                wf_engine.check_pause(run_ctx, node_id)
            resp = await llm_invoke_with_timeout(bound.ainvoke(msgs))
            msgs.append(resp)
            result_text = text_of_content(resp.content)
            # Per-call telemetry into the global usage log (source=workflow);
            # falls back to response_metadata parsing when the provider does
            # not populate usage_metadata (then nothing is logged — same
            # best-effort semantics as the chat path).
            u = ah.record_model_usage(resp, run_ctx.get("usage_attr")) or _usage_from_msg(resp)
            for k, v in u.items():
                usage[k] = usage.get(k, 0) + v
            calls = getattr(resp, "tool_calls", None) or []
            if not calls:
                break
            emit(
                {
                    "run_id": run_ctx["run_id"],
                    "node_id": node_id,
                    "kind": "tool_call",
                    "calls": [{"name": c.get("name"), "args": c.get("args")} for c in calls],
                }
            )
            if tool_node is None:
                break
            tres = await tool_node.ainvoke({"messages": [resp]})
            tmsgs = tres["messages"] if isinstance(tres, dict) else tres
            for tm in tmsgs:
                c = text_of_content(tm.content)
                emit(
                    {
                        "run_id": run_ctx["run_id"],
                        "node_id": node_id,
                        "kind": "tool_result",
                        "name": getattr(tm, "name", ""),
                        "content": c[:2000],
                    }
                )
            msgs.extend(tmsgs)

        writes = ah.parse_writes(result_text)
        meta = dict(state.get("context_meta") or {})
        for k in writes:
            meta[k] = f"step:{node_id}"
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done", "usage": usage})
        if writes:
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "context_write", "keys": list(writes.keys())})
        return {
            "context": {**context, **writes},
            "context_meta": meta,
            "results": {**state.get("results", {}), node_id: result_text},
            "events": events,
            "__output__": writes,
        }


@register_node
class LLMNode(BaseNode):
    """Pure generation node (no tools): render a prompt, optionally store to context."""

    type = "llm"
    params_schema = {"type": "object", "required": ["prompt"], "properties": {"prompt": {"type": "string"}, "output": {"type": "string"}}}

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        model = cctx["model"]
        node_id = node["id"]
        context = dict(state.get("context") or {})
        prompt = wf_expr.render(node.get("prompt") or "", {**context, **(eff or {})})

        # Incremental flush (see AgentNode): mid-flight failures keep their
        # footprint; ``events`` still returns in the state update (reducer).
        events: list = []

        def emit(ev):
            ev.setdefault("ts", time.time())
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "llm"})
        resp = await llm_invoke_with_timeout(model.ainvoke([HumanMessage(content=prompt)]))
        text = text_of_content(resp.content)
        usage = ah.record_model_usage(resp, run_ctx.get("usage_attr")) or _usage_from_msg(resp)
        out = {"text": text}
        update = {"events": events, "__output__": out}
        key = node.get("output")
        if key:
            update["context"] = {**context, key: text}
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "context_write", "keys": [key]})
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done", "usage": usage})
        return update


@register_node
class BranchNode(BaseNode):
    """Conditional router: evaluate cases against context, first match wins, else default."""

    type = "branch"

    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        errs = []
        nid = node.get("id")
        cases = node.get("cases") or []
        if not cases and not node.get("default"):
            errs.append(f"branch '{nid}' needs cases or default")
        for j, c in enumerate(cases):
            if not isinstance(c, dict) or not c.get("when") or not c.get("then"):
                errs.append(f"branch '{nid}' case[{j}] needs when+then")
        return errs

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        from . import transforms as wf_transforms

        ctx = dict(state.get("context") or {})
        target = None
        transform = None
        for c in node.get("cases") or []:
            try:
                if wf_expr.eval_expr(c.get("when", ""), ctx):
                    target = c.get("then")
                    transform = c.get("transform")
                    break
            except Exception:
                continue
        if target is None:
            target = node.get("default")
            transform = node.get("default_transform")
        inputs = {}
        if target:
            inputs[target] = wf_transforms.apply_transform(transform, {}, ctx)
        return {"events": [], "inputs": inputs, "__output__": {}}

    @classmethod
    def add_edges(cls, g, node, d) -> None:
        nid = node["id"]
        targets = {c["then"] for c in (node.get("cases") or []) if c.get("then")}
        if node.get("default"):
            targets.add(node["default"])

        def route(state, config=None) -> str:
            return wf_expr.eval_branch(node, state.get("context") or {}) or END

        g.add_conditional_edges(nid, route, {t: t for t in targets} | {END: END})


@register_node
class LoopNode(BaseNode):
    """Iterate ``over`` (context list / int); each pass runs ``body``, back-edge to head."""

    type = "loop"

    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        errs = []
        nid = node.get("id")
        if not node.get("body"):
            errs.append(f"loop '{nid}' needs body")
        if not node.get("over"):
            errs.append(f"loop '{nid}' needs over")
        if not isinstance(node.get("max_iters"), int) or node.get("max_iters", 0) < 1:
            errs.append(f"loop '{nid}' needs max_iters >= 1")
        if node.get("on_empty") not in (None, "skip", "fail"):
            errs.append(f"loop '{nid}' on_empty must be 'skip' or 'fail'")
        return errs

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        node_id = node["id"]
        as_var = node.get("as") or "item"
        max_iters = int(node.get("max_iters") or 100)
        on_empty = node.get("on_empty") or "skip"  # "skip" | "fail"
        iters = dict(state.get("loop_iters") or {})
        st = dict(iters.get(node_id) or {"index": 0, "items": None, "done": False})
        context = dict(state.get("context") or {})
        loop_vars = dict(state.get("loop_vars") or {})
        if st["items"] is None:
            over = node.get("over")
            try:
                val = wf_expr.eval_expr(over, context) if isinstance(over, str) else over
            except Exception:
                val = []
            if isinstance(val, int):
                val = list(range(val))
            st["items"] = val if isinstance(val, list) else []
        idx = st["index"]
        items = st["items"]
        # Incremental flush (see AgentNode): loop_iter/node_enter are recorded
        # as produced; ``events`` still returns in the state update (reducer).
        events: list = []

        def emit(ev):
            ev.setdefault("ts", time.time())
            events.append(ev)
            run_ctx["events"].append(ev)

        # ---- on_empty: first pass with an empty sequence (master-plan §2.1) ----
        # Emit a visible loop_skip so the skip is never silent, then either
        # continue (skip) or fail the whole run (fail) attributed to this loop.
        if idx == 0 and len(items) == 0:
            over_expr = node.get("over")
            emit({
                "run_id": run_ctx["run_id"], "node_id": node_id, "kind": "loop_skip",
                "over": str(over_expr), "reason": "empty sequence", "on_empty": on_empty,
            })
            if on_empty == "fail":
                emit({
                    "run_id": run_ctx["run_id"], "node_id": node_id, "kind": "error",
                    "error": f"loop '{node_id}' over '{over_expr}' is empty and on_empty='fail'",
                })
                raise RuntimeError(f"loop '{node_id}' empty sequence with on_empty='fail'")
            st["done"] = True
            iters[node_id] = st
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done"})
            return {"loop_iters": iters, "loop_vars": loop_vars, "events": events}

        # ---- parallel gather (stability plan P3) ----
        # First pass hands the WHOLE item table to the body (loop_vars[as]=items)
        # and marks the loop parallel; the body's gather adapter does the
        # per-item work in one activation and the next pass closes the loop.
        # Gate off → degrade to sequential with a visible warning, never error.
        from ... import world_state as ws_mod
        from .. import dsl as wf_dsl

        par, _mc = wf_dsl.loop_parallel_spec(node)
        if par and not ws_mod.workflow_parallel_enabled():
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "warning",
                  "message": f"loop '{node_id}' 声明了 parallel，但 settings "
                  "workflow_parallel_loops 未开启，降级为顺序执行"})
            par = False
        if st.get("parallel"):
            st["done"] = True
            iters[node_id] = st
            # The batch table is no longer needed; drop it so downstream steps
            # don't render the whole list into their goals (sequential loops
            # leave the LAST item; parallel leaves nothing).
            loop_vars.pop(as_var, None)
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "loop"})
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done"})
            return {"loop_iters": iters, "loop_vars": loop_vars, "events": events}
        if par and idx == 0:
            # max_iters caps the batch exactly like it caps sequential passes.
            batch = items[:max_iters]
            if len(batch) < len(items):
                emit({
                    "run_id": run_ctx["run_id"], "node_id": node_id, "kind": "loop_cap",
                    "max_iters": max_iters, "remaining": len(items) - len(batch),
                })
            loop_vars[as_var] = batch
            # Per-item loop_iter events are emitted by the gather adapter as
            # each item starts (index/of/parallel) — same observable contract
            # as the sequential one-event-per-iteration flow.
            st = {"index": len(batch), "items": items, "done": False, "parallel": True}
            iters[node_id] = st
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "loop"})
            return {"loop_iters": iters, "loop_vars": loop_vars, "events": events}

        if idx < len(items) and idx < max_iters:
            loop_vars[as_var] = items[idx]
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "loop_iter", "index": idx, "of": len(items)})
            st["index"] = idx + 1
            st["done"] = False
        else:
            st["done"] = True
            # Hitting max_iters before exhausting items is a cap, not a clean end.
            if idx >= max_iters and idx < len(items):
                emit({
                    "run_id": run_ctx["run_id"], "node_id": node_id, "kind": "loop_cap",
                    "max_iters": max_iters, "remaining": len(items) - idx,
                })
        iters[node_id] = st
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "loop"})
        if st["done"]:
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done"})
        return {"loop_iters": iters, "loop_vars": loop_vars, "events": events}

    @classmethod
    def add_edges(cls, g, node, d) -> None:
        nid = node["id"]
        body = node.get("body")
        nxt = cls._outgoing(d, nid)  # the single "done/next" edge (validated)

        def route(state, config=None) -> str:
            st = ((state.get("loop_iters") or {}).get(nid)) or {}
            if st.get("done"):
                return nxt or END
            return body or END

        routes = {}
        if body:
            routes[body] = body
        if nxt:
            routes[nxt] = nxt
        g.add_conditional_edges(nid, route, routes | {END: END})
        if body:
            # If the body step declared ``writes``, the compiler injected a
            # ``body__extract`` node whose back_to points at this loop head;
            # that node wires the return edge itself, so we must NOT also add
            # body->head here (it would fork the body into two paths).
            body_extracted = any(
                isinstance(n, dict) and n.get("id") == f"{body}__extract"
                for n in d.get("nodes") or []
            )
            if not body_extracted:
                g.add_edge(body, nid)


@register_node
class HumanNode(BaseNode):
    """Human-in-the-loop checkpoint: suspends via ``interrupt`` and resumes with the
    provided value (e.g. ``{"decision": "continue", "context_patch": {...}}``)."""

    type = "human"
    params_schema = {"type": "object", "properties": {"question": {"type": "string"}}}

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        from langgraph.types import interrupt

        run_ctx = cctx["run_ctx"]
        run_ctx["events"].append(
            {"ts": time.time(), "run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "interrupt", "question": node.get("question")}
        )
        value = interrupt({"kind": "human", "node": node["id"], "question": node.get("question")})
        run_ctx["events"].append({"ts": time.time(), "run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "resume"})
        update: dict = {"events": []}
        # a context_patch in the resume value merges into context
        if isinstance(value, dict) and isinstance(value.get("context_patch"), dict):
            ctx = dict(state.get("context") or {})
            update["context"] = {**ctx, **value["context_patch"]}
        update["__output__"] = value if isinstance(value, dict) else {"resume": value}
        return update


@register_node
class BrowserNode(BaseNode):
    """First-class browser step (docs/browser-embed-design.md §9).

    ``action``: eval | snapshot | handoff | complete. ``complete`` cannot share a
    node with ``eval`` (ego PR #29). Same ``space`` name is reused for the run.
    """

    type = "browser"
    params_schema = {
        "type": "object",
        "properties": {
            "space": {"type": "string"},
            "action": {"type": "string"},
            "code": {"type": "string"},
            "keep": {"type": "boolean"},
            "timeout_s": {"type": "integer"},
            "headed": {"type": "boolean"},
        },
    }

    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        errs = super().validate_params(node)
        action = (node.get("action") or "eval").strip()
        if action not in ("eval", "snapshot", "handoff", "complete"):
            errs.append(f"browser '{node.get('id')}' action must be eval|snapshot|handoff|complete")
        if action == "complete" and (node.get("code") or "").strip():
            # complete may carry keep only — mixing work script is the ego #29 footgun
            if "handOff" in (node.get("code") or "") or "useOrCreate" in (node.get("code") or ""):
                errs.append(
                    f"browser '{node.get('id')}': complete cannot share a node with eval helpers"
                )
        if action == "eval" and not (node.get("code") or node.get("url")):
            errs.append(f"browser '{node.get('id')}' eval needs code (or url)")
        return errs

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        from langgraph.types import interrupt

        from ...browser import GLOBAL_SPACE, get_supervisor
        from ...browser.helpers import BrowserHandoff
        from ...browser.supervisor import BrowserLocked

        run_ctx = cctx["run_ctx"]
        run_id = run_ctx.get("run_id")
        session_id = run_ctx.get("present_in_session_id") or run_ctx.get("session_id")
        ctx = dict(state.get("context") or {})
        action = (node.get("action") or "eval").strip()
        space = wf_expr.render(node.get("space") or GLOBAL_SPACE, ctx).strip()
        code = wf_expr.render(node.get("code") or "", ctx)
        url = wf_expr.render(node.get("url") or "", ctx)
        timeout_s = int(node.get("timeout_s") or 180)
        headed = bool(node.get("headed", True))
        keep = bool(node.get("keep", True))
        sup = get_supervisor()
        run_ctx["events"].append(
            {
                "ts": time.time(),
                "run_id": run_id,
                "node_id": node["id"],
                "kind": "node_enter",
                "node_type": "browser",
                "space": space,
                "action": action,
            }
        )

        def _handoff_interrupt(space_name: str, page_url: str, reason: str) -> dict:
            run_ctx["events"].append(
                {
                    "ts": time.time(),
                    "run_id": run_id,
                    "node_id": node["id"],
                    "kind": "interrupt",
                    "nature": "browser_handoff",
                    "space": space_name,
                    "url": page_url,
                    "reason": reason,
                    "question": reason or "需要你在浏览器里操作",
                }
            )
            value = interrupt(
                {
                    "kind": "browser_handoff",
                    "node": node["id"],
                    "space": space_name,
                    "url": page_url,
                    "reason": reason,
                    "run_id": run_id,
                }
            )
            run_ctx["events"].append(
                {
                    "ts": time.time(),
                    "run_id": run_id,
                    "node_id": node["id"],
                    "kind": "resume",
                    "nature": "browser_handoff",
                }
            )
            try:
                sup.take_over(space_name)
            except Exception:
                pass
            return value if isinstance(value, dict) else {"resume": value}

        try:
            rec = sup.use_or_create(space, session_id=session_id, run_id=run_id)
            if action == "snapshot":
                snap = sup.snapshot(rec["name"])
                out = {
                    "space": rec["name"],
                    "url": snap.get("url"),
                    "title": snap.get("title"),
                    "snapshot": snap.get("text"),
                }
            elif action == "handoff":
                try:
                    sup.hand_off(rec["name"], reason=node.get("reason") or node.get("question") or "")
                    out = {"space": rec["name"]}
                except BrowserHandoff as h:
                    out = _handoff_interrupt(h.space, h.url, h.reason)
            elif action == "complete":
                out = sup.complete(rec["name"], keep=keep, from_complete_node=True)
            else:
                script = code
                if url and "openOrReuseTab" not in script and "gotoUrl" not in script:
                    script = f"await openOrReuseTab({url!r}, {{ wait: true }})\n" + script
                result = sup.eval(
                    script,
                    space=rec["name"],
                    session_id=session_id,
                    run_id=run_id,
                    timeout_s=timeout_s,
                    headed=headed,
                    allow_complete=False,
                )
                if isinstance(result, dict) and result.get("interrupt") == "handoff":
                    out = _handoff_interrupt(
                        result.get("space") or rec["name"],
                        result.get("url") or "",
                        result.get("reason") or "",
                    )
                elif isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(result["error"])
                else:
                    out = result if isinstance(result, dict) else {"return": result}
        except BrowserLocked as e:
            raise RuntimeError(str(e)) from e

        info = {}
        try:
            info = sup.page_info(space)
        except Exception:
            pass
        if isinstance(out, dict):
            out.setdefault("url", info.get("url"))
            out.setdefault("title", info.get("title"))
            out.setdefault("space", space)
        run_ctx["events"].append(
            {
                "ts": time.time(),
                "run_id": run_id,
                "node_id": node["id"],
                "kind": "node_exit",
                "node_type": "browser",
                "status": "ok",
                "space": space,
            }
        )
        update: dict = {"events": [], "__output__": out if isinstance(out, dict) else {"return": out}}
        writes = node.get("writes")
        if writes and isinstance(out, dict):
            ctx_update = dict(state.get("context") or {})
            # DSL `writes` is `{key: {type}}` (schema); a bare string is also accepted.
            keys: list[str]
            if isinstance(writes, dict):
                keys = list(writes.keys())
            elif isinstance(writes, list):
                keys = [str(w) for w in writes]
            elif isinstance(writes, str):
                keys = [writes]
            else:
                keys = []
            for w in keys:
                if w in out:
                    ctx_update[w] = out[w]
            if keys:
                update["context"] = ctx_update
        return update


@register_node
class PassNode(BaseNode):
    """No-op passthrough (useful for wiring/placeholder)."""

    type = "pass"
    aliases = ("noop",)

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        run_ctx["events"].append({"ts": time.time(), "run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "node_enter", "node_type": "pass"})
        return {"events": [], "__output__": {}}
