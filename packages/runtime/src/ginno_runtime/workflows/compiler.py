"""Compile a workflow DSL into a LangGraph StateGraph (design §5.1).

v1 node types: step + branch (loop/human/subflow are validated out until P3/P7).
The compiled graph is per-run: step-node closures capture the run's model,
filtered toolset and a ``run_ctx`` (run_id + event sink), so a single compile
serves one execution. The checkpointer is supplied by the engine at stream time.

A step node runs a small model+tool loop (no permission node — runs are
autonomous, design §5.3 / Q5) and writes structured fields back into
``state['context']`` via a ``WRITE_JSON`` fence in the model's final answer.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .. import agents as agents_reg
from ..graph import build_agent_system_prompt, tool_allowed
from . import dsl as wf_dsl
from . import expr as wf_expr

# Marker the step system prompt asks the model to use for context write-back.
WRITE_OPEN = "WRITE_JSON"
_WRITE_RE = re.compile(r"WRITE_JSON\s*(\{.*\})\s*", re.DOTALL)


class WorkflowState(TypedDict):
    context: dict
    context_meta: dict
    results: dict
    loop_iters: dict
    loop_vars: dict  # loop-scoped vars (the `as` item); merged into goal rendering only
    events: Annotated[list, lambda a, b: (a or []) + (b or [])]


def _outgoing(dsl: dict, node_id: str) -> str | None:
    for e in dsl.get("edges") or []:
        if e.get("from") == node_id:
            return e.get("to")
    return None


def _build_system(goal: str, context: dict, agent) -> str:
    base = build_agent_system_prompt(agent, "default", [], query="")
    return (
        f"{base}\n\n"
        "## Your step goal\n"
        f"{goal}\n\n"
        "## Current workflow context\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        "Use your tools as needed to achieve the goal. When you are done, if this "
        "step must update the workflow context, end your reply with a single JSON "
        "object on its own line prefixed by WRITE_JSON containing ONLY the fields to "
        "write, e.g. `WRITE_JSON {\"drafts\": [\"...\"]}`. Do not wrap it in code fences."
    )


def _extract_write_json(text: str) -> str | None:
    """Return the first brace-balanced JSON object following WRITE_OPEN.

    The old greedy regex spanned to the *last* ``}`` in the reply, so a normal
    answer like ``WRITE_JSON {"x":1}\\n\\nDone, config was {...}`` produced invalid
    JSON and silently dropped the step's context write-back (the core write path).
    A string-aware brace-balance scan fixes that.
    """
    i = text.find(WRITE_OPEN)
    if i < 0:
        return None
    j = text.find("{", i + len(WRITE_OPEN))
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[j : k + 1]
    return None


def _parse_writes(text: str) -> dict:
    if not text:
        return {}
    frag = _extract_write_json(text)
    if not frag:
        return {}
    try:
        data = json.loads(frag)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _loop_node_factory(node: dict, run_ctx: dict):
    """Iterate ``over`` (a context list or int range). On each pass inject the
    current item as ``context[as]`` and route to the body; when exhausted, exit.
    Loop state (index/items/done) lives in state['loop_iters'][node_id]; the
    body's write-back into context persists across iterations because the step
    node merges the full context dict."""
    node_id = node["id"]
    as_var = node.get("as") or "item"
    max_iters = int(node.get("max_iters") or 100)

    async def loop(state: WorkflowState, config=None) -> dict:
        iters = dict(state.get("loop_iters") or {})
        st = dict(iters.get(node_id) or {"index": 0, "items": None, "done": False})
        context = dict(state.get("context") or {})
        loop_vars = dict(state.get("loop_vars") or {})
        if st["items"] is None:  # first entry: resolve the collection
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
        events: list[dict] = []
        if idx < len(items) and idx < max_iters:
            # inject into the SEPARATE loop_vars channel so a body step's
            # context write-back (same superstep) cannot clobber it.
            loop_vars[as_var] = items[idx]
            events.append(
                {
                    "run_id": run_ctx["run_id"],
                    "node_id": node_id,
                    "kind": "loop_iter",
                    "index": idx,
                    "of": len(items),
                }
            )
            st["index"] = idx + 1
            st["done"] = False
        else:
            st["done"] = True
        iters[node_id] = st
        events.append(
            {"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "loop"}
        )
        run_ctx["events"].extend(events)
        return {"loop_iters": iters, "loop_vars": loop_vars, "events": events}

    return loop


def _loop_router(node: dict):
    node_id = node["id"]

    def route(state: WorkflowState, config=None) -> str:
        st = ((state.get("loop_iters") or {}).get(node_id)) or {}
        return END if st.get("done") else node.get("body") or END

    return route


def _step_node_factory(node: dict, model, tools: list, run_ctx: dict):
    node_id = node["id"]
    max_iters = int(node.get("max_tool_iters") or 8)

    async def step(state: WorkflowState, config=None) -> dict:
        agent = agents_reg.get_agent(node.get("agent"))
        # Strip workflow-management tools from a step's toolset: a step has no
        # legitimate need for them, and leaving workflow_propose_edit /
        # workflow_run in would let the step model fire an interrupt (orphaned
        # inside the engine → run silently reports "done" with lost output) or
        # spawn an orphan nested run.
        allowed = [
            t
            for t in tools
            if tool_allowed(agent, t.name) and not t.name.startswith("workflow_")
        ]
        bound = model.bind_tools(allowed) if allowed and hasattr(model, "bind_tools") else model
        tool_node = ToolNode(allowed) if allowed else None
        context = dict(state.get("context") or {})
        loop_vars = dict(state.get("loop_vars") or {})
        render_ctx = {**context, **loop_vars}  # loop `as` vars shadow context for templating
        goal = wf_expr.render(node.get("goal") or node.get("title") or "", render_ctx)
        events: list[dict] = [
            {"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "step"}
        ]

        msgs: list = [SystemMessage(content=_build_system(goal, context, agent)), HumanMessage(content=goal)]
        result_text = ""
        for _ in range(max_iters):
            resp: AIMessage = await bound.ainvoke(msgs)
            msgs.append(resp)
            result_text = resp.content if isinstance(resp.content, str) else str(resp.content)
            calls = getattr(resp, "tool_calls", None) or []
            if not calls:
                break
            events.append(
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
                c = tm.content if isinstance(tm.content, str) else str(tm.content)
                events.append(
                    {
                        "run_id": run_ctx["run_id"],
                        "node_id": node_id,
                        "kind": "tool_result",
                        "name": getattr(tm, "name", ""),
                        "content": c[:2000],
                    }
                )
            msgs.extend(tmsgs)

        writes = _parse_writes(result_text)
        meta = dict(state.get("context_meta") or {})
        for k in writes:
            meta[k] = f"step:{node_id}"
        events.append(
            {"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done"}
        )
        if writes:
            events.append(
                {
                    "run_id": run_ctx["run_id"],
                    "node_id": node_id,
                    "kind": "context_write",
                    "keys": list(writes.keys()),
                }
            )
        run_ctx["events"].extend(events)
        return {
            "context": {**context, **writes},
            "context_meta": meta,
            "results": {**state.get("results", {}), node_id: result_text},
            "events": events,
        }

    return step


def _branch_router(node: dict):
    node_id = node["id"]

    def route(state: WorkflowState, config=None) -> str:
        target = wf_expr.eval_branch(node, state.get("context") or {})
        return target or END

    return route


def compile_workflow(dsl: dict, model, tools: list, run_ctx: dict, checkpointer=None):
    """Compile a validated DSL into a StateGraph bound to this run's deps."""
    d = wf_dsl.normalize_dsl(dsl)
    errs = wf_dsl.validate_dsl(d)
    if errs:
        raise ValueError("invalid DSL: " + "; ".join(errs))
    by_id = {n["id"]: n for n in d["nodes"]}

    g = StateGraph(WorkflowState)
    for n in d["nodes"]:
        nt = n["type"]
        if nt == "step":
            g.add_node(n["id"], _step_node_factory(n, model, tools, run_ctx))
        elif nt == "branch":
            g.add_node(n["id"], lambda state, config=None, _n=n: {"events": []})
        elif nt == "loop":
            g.add_node(n["id"], _loop_node_factory(n, run_ctx))
        else:  # human/subflow rejected by validate_dsl in v1
            raise ValueError(f"node type '{nt}' not compiled in v1")

    for n in d["nodes"]:
        nid = n["id"]
        if n["type"] == "branch":
            targets = {c["then"] for c in (n.get("cases") or []) if c.get("then")}
            if n.get("default"):
                targets.add(n["default"])
            g.add_conditional_edges(nid, _branch_router(n), {t: t for t in targets} | {END: END})
        elif n["type"] == "loop":
            body = n.get("body")
            routes = {body: body} if body else {}
            g.add_conditional_edges(nid, _loop_router(n), routes | {END: END})
            # the body→loop back-edge is structural: synthesize it so the body
            # returns control to the loop head (the DSL must NOT declare it).
            if body:
                g.add_edge(body, nid)
        else:
            nxt = _outgoing(d, nid)
            g.add_edge(nid, nxt or END)

    g.add_edge(START, d["entry"])
    return g.compile(checkpointer=checkpointer)
