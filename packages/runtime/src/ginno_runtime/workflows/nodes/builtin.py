"""Built-in workflow node types (design A · round 3).

These are the *general-purpose* nodes shipped with Ginno; more can be added as
plugins without touching core (see :mod:`registry`). ``step`` is kept as an alias
of ``agent`` so existing DSLs and tests keep working.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.prebuilt import ToolNode

from ... import agents as agents_reg
from ...graph import tool_allowed
from .. import expr as wf_expr
from . import agent_helpers as ah
from .base import BaseNode
from .registry import register_node


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
        run_ctx = cctx["run_ctx"]
        model = cctx["model"]
        tools = cctx["tools"]
        node_id = node["id"]
        max_iters = int(node.get("max_tool_iters") or 8)
        agent = agents_reg.get_agent(node.get("agent"))
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
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "step"})
        msgs = [SystemMessage(content=sys_text), HumanMessage(content=goal)]
        result_text = ""
        for _ in range(max_iters):
            resp = await bound.ainvoke(msgs)
            msgs.append(resp)
            result_text = resp.content if isinstance(resp.content, str) else str(resp.content)
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
                c = tm.content if isinstance(tm.content, str) else str(tm.content)
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
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done"})
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
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "llm"})
        resp = await model.ainvoke([HumanMessage(content=prompt)])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        out = {"text": text}
        update = {"events": events, "__output__": out}
        key = node.get("output")
        if key:
            update["context"] = {**context, key: text}
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "context_write", "keys": [key]})
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit", "status": "done"})
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
        return errs

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        node_id = node["id"]
        as_var = node.get("as") or "item"
        max_iters = int(node.get("max_iters") or 100)
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
            events.append(ev)
            run_ctx["events"].append(ev)

        if idx < len(items) and idx < max_iters:
            loop_vars[as_var] = items[idx]
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "loop_iter", "index": idx, "of": len(items)})
            st["index"] = idx + 1
            st["done"] = False
        else:
            st["done"] = True
        iters[node_id] = st
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter", "node_type": "loop"})
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
            {"run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "interrupt", "question": node.get("question")}
        )
        value = interrupt({"kind": "human", "node": node["id"], "question": node.get("question")})
        run_ctx["events"].append({"run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "resume"})
        update: dict = {"events": []}
        # a context_patch in the resume value merges into context
        if isinstance(value, dict) and isinstance(value.get("context_patch"), dict):
            ctx = dict(state.get("context") or {})
            update["context"] = {**ctx, **value["context_patch"]}
        update["__output__"] = value if isinstance(value, dict) else {"resume": value}
        return update


@register_node
class PassNode(BaseNode):
    """No-op passthrough (useful for wiring/placeholder)."""

    type = "pass"
    aliases = ("noop",)

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        run_ctx["events"].append({"run_id": run_ctx["run_id"], "node_id": node["id"], "kind": "node_enter", "node_type": "pass"})
        return {"events": [], "__output__": {}}
