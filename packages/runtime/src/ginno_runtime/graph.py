"""Main LangGraph: START → agent → permission → tools → agent (loop).

One compiled graph per session carries the UNION toolset. The per-turn
agent (state['agent_id']) selects: the persona system prompt, the tool
subset bound to the model, and the tools_allow enforcement in the
permission node. The system prompt is rebuilt every turn and is NOT
persisted into the checkpoint, so switching agents mid-session (manual
"Ask X" routing) takes effect on the very next turn while the message
history stays shared across agents in the same conversation.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from typing import Literal
from xml.sax.saxutils import escape as _xml_escape

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from . import agents as agents_reg
from .checkpointer import FileCheckpointer
from .graph_messages import (
    IMAGE_KEEP_TURNS,
    STEER_HEAD,
    _annotate_turn_tools,
    _is_image_block,
    _latest_human_text,
    _steer_wrapped_text,
    _wrap_steered_for_model,
    collect_turn_images,
    is_midturn_injected,
    strip_old_images,
    strip_tool_code_markers,
    strip_tool_image_markers,
    text_of_content,
)
from .graph_models import (
    _CACHE_MARK,
    CACHE_TAIL_MARKS,
    _is_anthropic_model,
    _is_rate_limit_error,
    _mark_cache_tail,
    _system_message,
    _with_cache_mark,
    is_retryable_error,
)
from .graph_prompts import (
    _LAZY_TOOL_PREFIXES,
    _allowed_tool_names,
    _format_bound_workflow,
    _read_global_memory,
    build_agent_system_prompt,
    build_stable_system,
    build_turn_context,
    tool_allowed,
)
from .lang import response_lang_directive, t
from .permission.policy import PermissionPolicy, is_bypass_permissions
from .server_shared import STEER_CONTEXT_KEY, steer_drain, steer_messages
from .state import AgentState
from .tools.artifact_tools import ALL_ARTIFACT_TOOLS, ARTIFACT_TOOL_NAMES
from .tools.ask_tools import ASK_TOOL_NAMES, build_ask_tools
from .tools.builtin import build_builtin_tools
from .tools.document_tools import ALL_DOCUMENT_TOOLS
from .tools.goal_tools import GOAL_TOOL_NAMES
from .tools.render_tools import RENDER_TOOL_NAMES, attach_ref, render_widget
from .tools.skill_tools import SKILL_TOOL_NAMES, build_skill_tools
from .tools.todo_tools import ALL_TODO_TOOLS, TODO_TOOL_NAMES
from .tools.workflow_tools import (
    ALL_WORKFLOW_DEV_TOOLS,
    ALL_WORKFLOW_TOOLS,
    WORKFLOW_DEV_TOOL_NAMES,
    WORKFLOW_TOOL_NAMES,
)
from .truncation import truncate_tool_content
from .world_state import SessionCtx, WorldState, context_settings

_log = logging.getLogger(__name__)

# permission-node deny messages are tagged so the WS layer can resolve the
# matching "running" tool bubble (the model never streams these).
BLOCK_PREFIX = "[blocked:"


def _resolve_agent(agent_id: str | None):
    if agent_id:
        a = agents_reg.get_agent(agent_id)
        if a:
            return a
    lst = agents_reg.list_agents()
    return lst[0] if lst else None


def _skill_extra_allow(state: dict | None) -> list[str]:
    """Union of `tools:` declared by currently active skills.

    A skill that needs bash/write (e.g. aliyun-bill) can therefore run even
    when the persona is a read-only analyst — but only after use_skill (or
    a slash invoke) has put the skill on active_skills this turn.
    """
    names = list((state or {}).get("active_skills") or [])
    names += [n for n in (state or {}).get("sticky_skills") or [] if n not in names]
    if not names:
        return []
    # Lazy toolset activation (browser-skill design, generalized): a skill
    # marked `sticky: true` rides sticky_skills for the whole session (the WS
    # layer resets active_skills every turn but never re-sends sticky_skills),
    # so ONE use_skill keeps its `tools:` family — browser_*, todo_* — bound.
    # connectors_deny still outranks all of this (tool_allowed checks it
    # BEFORE extra_allow).
    slug = (state or {}).get("project_slug") or None
    from .skills.loader import SkillLoader

    loader = SkillLoader(project_slug=slug)
    extra: list[str] = []
    seen: set[str] = set()
    for name in names:
        skill = loader.get(name)
        if not skill:
            continue
        for pat in skill.effective_tools():
            if pat and pat not in seen:
                seen.add(pat)
                extra.append(pat)
    return extra


# Degenerate-final-reply recovery (见 agent_node_factory 内注释):thinking 模型
# reasoning-only 收尾时的有界续跑。
_EMPTY_REPLY_MAX_NUDGES = 3
# Rate-limit backoff(2026-10-16,office-bench run4/5):429 类错误不该杀死
# turn——SDK/代理侧重试耗尽后异常仍会冒到 agent_node,这里再兜一层指数
# 退避,把「限流即失败」变成「限流即变慢」。
_RATE_LIMIT_MAX_WAITS = 5
_RATE_LIMIT_WAIT_S = 30.0  # 30/60/120/240/480s,共 ~15 分钟


async def _ainvoke_rate_limited(bound, msgs):
    waited = 0
    while True:
        try:
            return await bound.ainvoke(msgs)
        except Exception as exc:  # noqa: BLE001 — 只吞限流,其余照抛
            if waited >= _RATE_LIMIT_MAX_WAITS or not _is_rate_limit_error(exc):
                raise
            delay = _RATE_LIMIT_WAIT_S * (2 ** waited)
            waited += 1
            _log.warning(
                "rate_limited_backoff wait=%.0fs attempt=%d/%d err=%.200s",
                delay, waited, _RATE_LIMIT_MAX_WAITS, exc,
            )
            # 每轮等待把 provider 原始错误全文透传前端（同心跳的 custom 通道，
            # e48be8a3 实录：静默退避让「套餐无权限」看起来像卡死）。用户
            # 看着原始错误决定要不要 ⏹ 中断换模型。writer 不可用时退化静默。
            try:
                from langgraph.config import get_stream_writer

                writer = get_stream_writer()
                if writer is not None:
                    writer(
                        {
                            "ginno_model_error": {
                                "error": f"{type(exc).__name__}: {exc}"[:500],
                                "wait_s": delay,
                                "attempt": waited,
                                "max": _RATE_LIMIT_MAX_WAITS,
                            }
                        }
                    )
            except Exception:  # noqa: BLE001 — 透传失败不影响退避本身
                pass
            await asyncio.sleep(delay)
_EMPTY_REPLY_NUDGE = (
    "[system] Your last reply was degenerate — it carried reasoning but no "
    "visible answer and no tool calls. Do NOT deliberate further. Either call "
    "the one tool you need next, or output the final answer as normal message "
    "text immediately, in at most a few sentences."
)


def _turn_agent_id(state: AgentState, config) -> str | None:
    # config['configurable'] is injected reliably every step (including after a
    # permission interrupt resume), unlike the input dict on a continued thread.
    cfg = (config or {}).get("configurable") or {}
    return cfg.get("agent_id") or state.get("agent_id")


def agent_node_factory(model_getter, all_tools, router=None, tool_selector=None):
    """``model_getter``：无参可调用，返回当前模型对象。本节点在每个
    superstep 入口（即每次 LLM 调用前）解析一次真实实例——模型热切
    （model-assignment-design.md §3.2）靠它生效：PATCH 原地换掉会话条目
    的 ``model`` 后，这里下一次解析自然拿到新模型。后续所有用法
    （bind_tools / _is_anthropic_model / ainvoke）都作用于解析出的实例，
    所以类型检查与 provider 特性判断不受影响。

    ``router`` / ``tool_selector``（classifier/middlewares.py）：可选的
    classifier 中间件。router 在解析之后、bind_tools 之前返回（可能不同的）
    模型实例——沿用上面同一契约；tool_selector 在 ``tool_allowed`` 之后
    收窄本 superstep 绑定的工具子集。两者都自带降级语义：任何失败/低置信度
    返回原模型 / 全量列表，绝不抛异常打断 turn。"""

    async def agent_node(state: AgentState, config=None) -> dict:
        model = model_getter()
        if router is not None:
            model = await router.route(model, state, config)
        agent = _resolve_agent(_turn_agent_id(state, config))
        extra = _skill_extra_allow(state)
        allowed = [t for t in all_tools if tool_allowed(agent, t.name, extra)]
        tools_filtered = False
        if tool_selector is not None:
            selected = await tool_selector.select(state, allowed)
            tools_filtered = len(selected) != len(allowed)
            allowed = selected
        bound = (
            model.bind_tools(allowed)
            if allowed and hasattr(model, "bind_tools")
            else model
        )
        # Stable system layer rebuilt from live WorldState sections (plan C1):
        # byte-identical across turns unless a section actually changed.
        # extra_allow (active skill tools) is the one per-turn exception —
        # listing those tools here is what lets the model call them after
        # use_skill without violating "Never call a tool outside this list".
        sys_msg = _system_message(
            build_stable_system(
                agent,
                state.get("project_slug", ""),
                all_tools,
                agent_id=_turn_agent_id(state, config),
                mcp_tool_names=state.get("mcp_tool_names") or [],
                session_id=((config or {}).get("configurable") or {}).get("thread_id", ""),
                workspace=state.get("workspace", "") or "",
                context_dirs=state.get("context_dirs") or [],
                primary_path=state.get("primary_path", "") or "",
                extra_allow=extra,
            ),
            model,
        )
        # Steering (docs/steering-design.md §3.2). This node runs at the head of
        # every superstep — immediately before a model request — so a message the
        # user sent while the turn was running is absorbed HERE and reaches the
        # model within the SAME turn, positioned right after the tool results
        # that were streaming when they typed it. The superstep boundary IS
        # Claude Code's "as soon as those tool calls finish" moment.
        #
        # New ids only, never a reused turn id: the file checkpointer's delta
        # compares message ids and never contents (checkpointer.py
        # _try_messages_delta), so an id that already exists with different text
        # makes the delta store nothing and leaves the OLD text in rebuilt
        # history — silently.
        sid = ((config or {}).get("configurable") or {}).get("thread_id", "")
        _drained = steer_drain(sid) if sid else []
        # Attachments were converted to their MODEL-facing shapes at enqueue
        # time (stream._prepare_steer_payload) and ride the entry as
        # ``content_blocks`` / ``context_text``; this node only assembles them.
        # Nothing here may reach for the file registry: a node that ran file IO
        # would block the event loop inside the graph, and importing
        # api.stream from here would be an import cycle (stream imports graph).
        # The one shape both this node and the heal path must produce lives in
        # server_shared.steer_messages.
        steered: list[HumanMessage] = steer_messages(_drained)
        if steered:
            # Acknowledge at DRAIN time, through an emitter the stream layer
            # injects into config (graph.py never touches the WebSocket itself).
            # The timing is load-bearing: the ack positions the transcript band,
            # which belongs at the injection point — right after the tool batch
            # that was running — and this superstep's tokens stream immediately
            # after. Acking at commit instead put the band AFTER the model's
            # continuation (found by driving the real UI), and the replayed view
            # reads the state order, so the two disagreed.
            _ack = ((config or {}).get("configurable") or {}).get("steer_absorbed")
            if callable(_ack):
                await _ack(_drained)
        base = [*state.get("messages", []), *steered]
        history = [m for m in base if not isinstance(m, SystemMessage)]
        # Trim old turns' images on a COPY so the LLM context stays bounded while
        # the persisted state keeps every image (UI history / time-travel intact).
        history = strip_old_images(history)
        # Hide code-generated-image markers from the model (display-only); the
        # persisted ToolMessages keep them for the WS layer / history builder.
        history = strip_tool_image_markers(history)
        # Same for the code-panel change trailer (write_file / edit_file):
        # machine metadata, display/transport only.
        history = strip_tool_code_markers(history)
        # Tell the model that these arrived mid-turn (copy-only; see the helper).
        history = _wrap_steered_for_model(history)
        # Classifier tool-selection narrowed the set below the stable system
        # prompt's declaration — the per-turn truth rides on the tail message
        # (copy-only) so the stable layer stays byte-identical for the cache.
        if tools_filtered:
            history = _annotate_turn_tools(history, allowed)
        # B3-tail — rolling cache breakpoints so the growing history caches too
        # (not just system+tools). Same gate as _system_message; the copy
        # semantics match the strip_* steps above (persisted state untouched).
        if _is_anthropic_model(model) and context_settings().get("cache_control", True):
            history = _mark_cache_tail(history)
        response = await _ainvoke_rate_limited(bound, [sys_msg] + history)
        # Degenerate-final-reply recovery (2026-10-16, office-bench 根因):
        # thinking 模型偶尔以「reasoning-only」消息收尾——content 为空、无
        # tool_calls、finish=stop。对图来说这就是正常 END,turn 静默结束、零
        # 可见产出。有界续跑:把空回复 + 提醒注入上下文再问一次,最多
        # _EMPTY_REPLY_MAX_NUDGES 次;全部失败才照旧 END。空回复与提醒一并
        # 落盘(重放一致),只在本次 superstep 的调用输入上追加。
        empty_tail: list = []
        for _ in range(_EMPTY_REPLY_MAX_NUDGES):
            # 退化判定:无 tool_calls 且文本为空或 <2 字符——thinking 模型截断
            # 后除了空 content 还会吐单字符残答("I"),对 benchmark 一样是废答。
            if (getattr(response, "tool_calls", None) or []) or len(
                text_of_content(getattr(response, "content", "")).strip()
            ) >= 2:
                break
            empty_tail += [response, HumanMessage(content=_EMPTY_REPLY_NUDGE)]
            response = await _ainvoke_rate_limited(bound, [sys_msg] + history + empty_tail)
        # Attribution tag for history replay (C+ plan): messages_ui reads this
        # to label each bubble with the agent that actually answered. Lives in
        # additional_kwargs because langchain-core 1.x BaseMessage has no
        # metadata field; round-trips through the checkpointer serde and is
        # ignored by provider payload converters.
        aid = _turn_agent_id(state, config)
        if aid:
            response.additional_kwargs["agent_id"] = aid
        # Durable anchor for code-generated images this turn (bash markers):
        # microcompact clears old ToolMessage bodies but never additional_kwargs,
        # so the history builder can still re-emit the images on replay.
        turn_imgs = collect_turn_images(state.get("messages", []))
        if turn_imgs:
            response.additional_kwargs["ginno_images"] = turn_imgs
        tool_calls = getattr(response, "tool_calls", None) or []
        # Steered messages ride out WITH the response so they commit to state in
        # one update, ordered before it: [..., tool results, steer, response].
        # empty_tail(空回复+提醒)同样按序落在 response 之前。
        return {
            "messages": [*steered, *empty_tail, response],
            "pending_tool_calls": tool_calls,
        }

    return agent_node


def permission_node_factory(policy: PermissionPolicy, hook_dispatcher, all_tools):
    async def permission_node(state: AgentState, config=None) -> Command:
        from .hooks.dispatcher import HookEvent

        # Privileged mode skips per-agent tools_allow + the permission policy, but
        # PreToolUse hooks still run (they are user-authored rules, authoritative).
        # With no hooks configured (the default) this means every tool runs freely.
        bypass = is_bypass_permissions()
        extra = _skill_extra_allow(state)
        agent = _resolve_agent(_turn_agent_id(state, config))
        # Attribution tag for deny/blocked bubbles below (see agent_node).
        aid = _turn_agent_id(state, config)
        pending = list(state.get("pending_tool_calls") or [])
        # The AIMessage that issued these calls (the latest one carrying
        # tool_calls): mods {args} rewrites and {result} takeovers must amend
        # IT — the tools node executes the latest AIMessage's tool_calls, not
        # the pending list. model_copy keeps the message id, so add_messages
        # replaces it in place instead of duplicating it.
        ai_msg = next(
            (
                m
                for m in reversed(state.get("messages") or [])
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
            ),
            None,
        )
        _ai_changed = False
        _extra_msgs: list = []  # takeover ToolMessages + mod-injected context
        # 本 turn 的 session id（thread_id == 会话 id）；定时任务会话不加载
        # Claude Mods 的 plugin hooks（mods/events.py 同口径，2026-10-28）。
        _thread_id = str(((config or {}).get("configurable") or {}).get("thread_id") or "")
        _plugin_hooks_ok = True
        if _thread_id:
            try:
                from .session_meta import session_is_scheduled

                _plugin_hooks_ok = not session_is_scheduled(_thread_id)
            except Exception:  # noqa: BLE001 — 判定失败按普通会话处理
                _plugin_hooks_ok = True
        for tc in list(pending):
            name = tc.get("name", "")
            args = tc.get("args", {})

            # structured-output tools never need permission / hooks
            if name in RENDER_TOOL_NAMES:
                continue

            # 0) per-agent tools_allow enforcement (skipped under bypass)
            if not bypass and not tool_allowed(agent, name, extra):
                return Command(
                    goto="agent",
                    update={
                        "messages": [
                            AIMessage(
                                content=(
                                    f"{BLOCK_PREFIX}{name}] "
                                    + t(
                                        f"{name} is not available to "
                                        f"{agent.name if agent else 'this agent'}. "
                                        "Use one of your available tools, or answer directly.",
                                        f"{name} 不可用于 "
                                        f"{agent.name if agent else 'this agent'}。"
                                        "请改用你可用的工具，或直接回答。",
                                    )
                                ),
                                additional_kwargs={"agent_id": aid} if aid else {},
                            )
                        ]
                    },
                )

            # TODO tools: an agent that has them (per tools_allow) never needs a prompt
            if name in TODO_TOOL_NAMES:
                continue
            # Goal tools are the same class: they only manage Ginno's own goal
            # store and must never interrupt autonomous continuation with an
            # approval prompt (goal-design.md §4.2).
            if name in GOAL_TOOL_NAMES:
                continue
            # workflow / artifact tools never need a prompt either. workflow-dev
            # editing tools carry their OWN diff confirmation (interrupt), so they
            # must bypass the permission policy too — otherwise the policy's "ask"
            # would fire a permission.request before the tool's version_propose.
            # Skill tools are the same class while they manage Ginno's own
            # storage (~/.ginno/skills) — but install_skills(target="repo")
            # writes into the USER'S repo, which is a file-write capability and
            # must go through the policy like any other write.
            # ask_user is the same class again: it carries its OWN interrupt,
            # so a policy "ask" would fire a permission.request AHEAD of it and
            # the user would answer two prompts for one question.
            skill_owned = name in SKILL_TOOL_NAMES and not (
                name == "install_skills"
                and str(args.get("target") or "") == "repo"
            )
            if (
                name in WORKFLOW_TOOL_NAMES
                or name in ARTIFACT_TOOL_NAMES
                or name in WORKFLOW_DEV_TOOL_NAMES
                or skill_owned
                or name in ASK_TOOL_NAMES
            ):
                continue

            # 1) PreToolUse hooks（定时任务会话跳过 plugin hooks，settings hooks 照跑）
            if hook_dispatcher:
                results = await hook_dispatcher.dispatch(
                    HookEvent(name="PreToolUse", context={"tool": name, "args": args}),
                    matcher=name,
                    plugin_hooks=_plugin_hooks_ok,
                )
                for r in results:
                    if r.block:
                        return Command(
                            goto="agent",
                            update={
                                "messages": [
                                    AIMessage(
                                        content=f"{BLOCK_PREFIX}{name}] hook blocked: {r.reason}",
                                        additional_kwargs={"agent_id": aid} if aid else {},
                                    )
                                ]
                            },
                        )

            # 1.5) Mods tool.call (claude-code-mods-design.md §5.3/§15.5):
            # same position as the classic PreToolUse hooks — still BEFORE the
            # permission policy. Consumption contract (P1):
            #   {"deny": reason} → blocked bubble, same shape as the hooks block;
            #   {"args": {...}}  → substitute the rewritten args AND re-run the
            #                      policy on them; the tools node must execute
            #                      the rewritten call too, so the issuing
            #                      AIMessage gets a same-id replacement;
            #   {"result": {"value", "isError"?}} → takeover: the tool never
            #                      runs, value becomes its ToolMessage (error
            #                      status when isError);
            #   {"inject": text} → extra context, appended to this turn's
            #                      message stream on the steer channel.
            # Priority deny > result > args; inject may coexist. Zero-cost
            # (short-circuit inside) when no mod is connected.
            _mod_action: dict | None = None
            try:
                from .mods.events import dispatch_tool_call

                _mod_action = await dispatch_tool_call(_thread_id, name, args)
            except Exception:
                _mod_action = None
            if isinstance(_mod_action, dict):
                if _mod_action.get("deny"):
                    return Command(
                        goto="agent",
                        update={
                            "messages": [
                                AIMessage(
                                    content=f"{BLOCK_PREFIX}{name}] mod denied: {_mod_action['deny']}",
                                    additional_kwargs={"agent_id": aid} if aid else {},
                                )
                            ]
                        },
                    )
                _took = _mod_action.get("result")
                if isinstance(_took, dict) and "value" in _took:
                    _tc_id = tc.get("id")
                    if ai_msg is not None and _tc_id:
                        from ginno_runtime import jsonx as _json

                        _val = _took.get("value")
                        _content = (
                            _val
                            if isinstance(_val, str)
                            else _json.dumps(_val, ensure_ascii=False, default=str)
                        )
                        _extra_msgs.append(
                            ToolMessage(
                                content=_content,
                                tool_call_id=_tc_id,
                                name=name,
                                status="error" if _took.get("isError") else "success",
                            )
                        )
                        ai_msg = ai_msg.model_copy(
                            update={"tool_calls": [c for c in ai_msg.tool_calls if c.get("id") != _tc_id]}
                        )
                        pending = [p for p in pending if p.get("id") != _tc_id]
                        _ai_changed = True
                        # Taken over: no policy, no execution for this call.
                        continue
                    # No id / no AIMessage to amend: a takeover we cannot
                    # represent must not silently swallow the call — run it.
                _rewritten = _mod_action.get("args")
                if isinstance(_rewritten, dict) and _rewritten and _rewritten != args:
                    _tc_id = tc.get("id")
                    if ai_msg is not None and _tc_id:
                        tc = {**tc, "args": _rewritten}
                        pending = [p if p.get("id") != _tc_id else tc for p in pending]
                        ai_msg = ai_msg.model_copy(
                            update={
                                "tool_calls": [
                                    {**c, "args": _rewritten} if c.get("id") == _tc_id else c
                                    for c in ai_msg.tool_calls
                                ]
                            }
                        )
                        args = _rewritten  # the policy below re-decides on these
                        _ai_changed = True
                _inject = _mod_action.get("inject")
                if isinstance(_inject, str) and _inject.strip():
                    _extra_msgs.append(
                        HumanMessage(
                            content=(
                                f'Message from the "{_mod_action["mod"]}" mod:\n{_inject}'
                                if _mod_action.get("mod")
                                else _inject
                            ),
                            additional_kwargs={
                                STEER_CONTEXT_KEY: {
                                    "origin": "mod",
                                    "mod": _mod_action.get("mod") or "",
                                }
                            },
                        )
                    )

            # 1.6) Mods tool.check (§5.3, P1): dispatched after the tool.call
            # chain, BEFORE policy.decide. allow → fall through to the policy;
            # deny → blocked bubble; ask → deny for now with the reason logged
            # (its confirmation UI lands later on the $.ui.ask base).
            _mod_check: dict | None = None
            try:
                from .mods.events import dispatch_tool_check

                _mod_check = await dispatch_tool_check(_thread_id, name, args)
            except Exception:
                _mod_check = None
            if isinstance(_mod_check, dict):
                _decision = str(_mod_check.get("decision") or "")
                _reason = str(_mod_check.get("reason") or "denied by mod (tool.check)")
                if _decision in ("deny", "ask"):
                    if _decision == "ask":
                        import logging as _logging

                        _logging.getLogger("ginno.mods").warning(
                            "mods tool.check ask treated as deny session=%s tool=%s reason=%s",
                            _thread_id,
                            name,
                            _reason,
                        )
                    return Command(
                        goto="agent",
                        update={
                            "messages": [
                                AIMessage(
                                    content=f"{BLOCK_PREFIX}{name}] mod denied: {_reason}",
                                    additional_kwargs={"agent_id": aid} if aid else {},
                                )
                            ]
                        },
                    )

            # 2) permission policy (skipped under bypass)
            if not bypass:
                decision = policy.decide(name, repr(args))
                if decision == "ask":
                    answer = interrupt({"kind": "permission_request", "tool": name, "args": args})
                    if answer.get("decision") == "deny":
                        return Command(
                            goto="agent",
                            update={
                                "messages": [
                                    AIMessage(
                                        content=f"{BLOCK_PREFIX}{name}] user denied",
                                        additional_kwargs={"agent_id": aid} if aid else {},
                                    )
                                ]
                            },
                        )
                elif decision == "deny":
                    return Command(
                        goto="agent",
                        update={
                            "messages": [
                                AIMessage(
                                    content=f"{BLOCK_PREFIX}{name}] policy denied",
                                    additional_kwargs={"agent_id": aid} if aid else {},
                                )
                            ]
                        },
                    )
        # Mods rewrote something (args / takeover / inject): flush the amended
        # AIMessage (same id → replaced in place), the takeover ToolMessages
        # and the injected context alongside the reduced pending list. All
        # calls taken over → back to the agent directly (nothing to execute).
        if _ai_changed or _extra_msgs:
            msgs = ([ai_msg] if _ai_changed and ai_msg is not None else []) + _extra_msgs
            return Command(
                goto="tools" if pending else "agent",
                update={"messages": msgs, "pending_tool_calls": pending},
            )
        return Command(goto="tools")

    return permission_node


def route_after_agent(state: AgentState) -> Literal["permission", "__end__"]:
    return "permission" if state.get("pending_tool_calls") else END


def _project_observer(project_slug: str | None, session_id: str | None):
    """Build the project-discovery hook for the file/shell tools (projects.py).

    Returns None without a session (workflow engine, listing endpoints) so
    those callers keep the pre-2026-09 behaviour. The note is emitted ONCE per
    project — ``record`` returns an entry only for a genuinely new root — so
    ordinary repos the agent works in stay quiet, and only a Claude-style
    project (``.claude`` / ``CLAUDE.md``) speaks up, because that is where the
    "install into the repo or into Ginno?" ambiguity actually lives.
    """
    if not (project_slug and session_id):
        return None
    from . import projects as projects_mod

    def _observe(p) -> str | None:
        entry = projects_mod.record(project_slug, session_id, p)
        if not entry or not entry.get("has_claude"):
            return None
        extra = (
            f", .claude/skills×{entry['claude_skills']}"
            if entry.get("claude_skills")
            else ""
        )
        return (
            f"\n\n[project] {entry['path']}"
            + t(
                f" ({', '.join(entry['markers'])}{extra}) recorded as a project root of "
                "this session; for \"import / install into the project\" requests it may "
                "be the target the user means — confirm first, do not default to Ginno's "
                "own directory.",
                f"（{', '.join(entry['markers'])}{extra}）"
                "已记录为本会话的项目根目录；涉及「导入/安装到项目里」时，它可能才是"
                "用户想的目标——先确认，不要默认装进 Ginno 自己的目录。",
            )
        )

    return _observe


def _server_web_search_active() -> bool:
    """True when the *currently selected* provider searches the web itself.

    Kept in lockstep with models.server_web_search_on — both read the same
    provider config, so the tool we skip here is exactly the one the gateway
    replaces. Any failure (no settings yet, provider mid-edit) falls back to
    Gin's own tool: the model can then search, which is the safer default.
    """
    try:
        from . import models as models_mod
        from . import providers as prov_mod

        pid = prov_mod.get_default_config()
        cfg = prov_mod.get_config(pid) if pid else None
        return bool(cfg) and models_mod.server_web_search_on(cfg)
    except Exception:
        return False


def build_all_tools(
    mcp_tools: list | None = None,
    workspace: str | None = None,
    project_slug: str | None = None,
    session_id: str | None = None,
    context_dirs: list[dict] | None = None,
    primary_path: str | None = None,
    subagent_depth: int | None = None,
    restrict_tools: list[str] | None = None,
) -> list:
    """The union toolset shared by the main chat graph and the workflow engine.

    ``workspace`` / ``project_slug`` are bound into the file and skill tools
    at construction (plan F1) — sessions pass their own values; callers
    without a session context (workflow runs, listing endpoints) pass none
    and those tools keep the process-cwd fallback.

    ``session_id`` (with ``project_slug``) additionally binds the per-session
    goal tools (goal-design.md §4.2) and the subagent tools; callers without a
    session omit them.

    ``subagent_depth`` is THIS session's subagent depth (None = main
    conversation). At depth >= 2 ``spawn_subagent`` is structurally absent
    (subagent-design.md §4.2) — the cap is the toolset, not a runtime error.

    ``context_dirs`` / ``primary_path`` bind the session's mounted context
    folders into the builtin file/shell tools (context-folders-design.md).

    ``restrict_tools`` (P3 contract 1): fnmatch patterns a matched subagent
    type imposes on top of the parent persona's tools_allow — filtered from
    the BOUND toolset here (the persona filter itself keeps running at
    request time in agent_node/permission_node), so a restricted child never
    binds, advertises, or can reach the excluded tools at all.
    """
    from .tools.browser_tools import build_browser_tools
    from .tools.external_agent import build_external_agent_tools
    from .tools.goal_tools import build_goal_tools
    from .tools.mod_tools import build_mod_tools
    from .tools.plan_tools import build_plan_tools
    from .tools.subagent import build_subagent_tools
    from .tools.team import build_team_tools
    from .tools.web_tools import build_web_tools

    # Anthropic-protocol providers may search on the gateway itself (the
    # server-side web_search tool bound in models.py). Leave Gin's own
    # web_search unbound then: the public engines it scrapes are unreachable on
    # networks that need the gateway's search in the first place, and offering
    # both just makes the model search twice and fail once.
    web_tools = [] if _server_web_search_active() else build_web_tools(session_id)
    goal_tools = (
        build_goal_tools(project_slug, session_id)
        if (project_slug and session_id)
        else []
    )
    tools = (
        build_builtin_tools(
            workspace,
            context_dirs=context_dirs,
            primary_path=primary_path,
            observe_path=_project_observer(project_slug, session_id),
        )
        + build_skill_tools(project_slug, session_id, primary_path)
        # [] without a session_id (workflow engine / listing endpoints): no
        # socket to answer over, and a workflow has its own `human` node.
        + build_ask_tools(project_slug, session_id)
        + (mcp_tools or [])
        + [render_widget, attach_ref]
        + ALL_TODO_TOOLS
        + goal_tools
        + ALL_WORKFLOW_TOOLS
        + ALL_WORKFLOW_DEV_TOOLS
        + ALL_ARTIFACT_TOOLS
        + ALL_DOCUMENT_TOOLS
        # Web search/fetch (citations-design.md §4.2) — [] when disabled in
        # settings; session_id binds citation source registration. Also [] when
        # the provider searches server-side (see _server_web_search_active).
        + web_tools
        # Browser tools (browser-companion-extension-design.md §4) — [] when
        # disabled in settings; extension relay preferred, dedicated-profile
        # Chrome as fallback. browser_js / browser_file_upload deliberately
        # NOT in the permission exempt set (default ask).
        + build_browser_tools(session_id, workspace, context_dirs)
        # Mod-registered tools (claude-code-mods-design.md §5.4 P1) — [] when
        # no mod registered anything or no session (same socket convention as
        # the skill tools; broker's grant gate already vetted the registration).
        + build_mod_tools(session_id)
        # Delegation to external coding agents (external-agents-design.md) —
        # [] when disabled in settings; session_id/project_slug bind usage
        # attribution. Deliberately NOT in the permission exempt set.
        + build_external_agent_tools(workspace, session_id, project_slug)
        # Subagent lifecycle tools (subagent-design.md §5) — [] without a
        # session_id (workflow engine / listing endpoints); spawn_subagent is
        # dropped at depth >= 2 (structural cap). Also deliberately NOT in the
        # permission exempt set, same delegate_agent precedent: the user
        # approves the goal itself (policy default "ask").
        + build_subagent_tools(session_id, project_slug, subagent_depth=subagent_depth)
        # Lead 自主分派工具（agent-team-design §6 P3）——只绑团队主会话
        # （meta type == "team"），其余会话为空表。不进权限豁免集（同
        # spawn_subagent 先例：默认 ask，用户批准的是分派本身）。
        + build_team_tools(session_id)
        # 任务清单编排（plan-orchestration §5）：建单/改单/取消。无
        # session（workflow/列举端点）时为空，与 ask/subagent 同语义。
        + build_plan_tools(session_id, project_slug)
    )
    patterns = [p for p in (restrict_tools or []) if isinstance(p, str) and p.strip()]
    if patterns:
        tools = [
            t
            for t in tools
            if any(fnmatch.fnmatch(t.name, p) for p in patterns)
        ]
    return tools


# 工具心跳间隔（tools_node 内的 custom 心跳，见 _tools_node_factory 的
# 注释）：远小于 engine.CHUNK_TIMEOUT_S（180s），留足余量；模块级常量
# 供测试缩小。
TOOL_HEARTBEAT_S = 30.0


def _tool_error_reply(exc: Exception) -> str:
    """ToolNode 错误回喂文案(handle_tool_errors handler)。

    默认文案(langgraph 版本各异)是裸异常串——`Error: ToolInvocationError(
    "…path: Field required…") Please fix the error and try again.`——在定时
    任务回放里像出了大事(2026-10-21 用户报告:glm 两次把 read_file 参数发成
    空 {},回放满屏这个)。统一成内置工具的 `[error]` 风格:剥离包装、点名
    缺什么,模型照旧自愈,人看也不吓人。"""
    msg = str(exc)
    # ToolInvocationError 的 message 已含 "Error invoking tool 'x' with kwargs
    # … with error:\n <真因>"——取真因尾段。
    if "\n" in msg and "with error:" in msg:
        msg = msg.split("with error:", 1)[1].strip()
    first = msg.splitlines()[0].strip() if msg else type(exc).__name__
    return t(
        f"[error] tool call failed: {first} — check the arguments and retry "
        "with the required fields filled in.",
        f"[error] 工具调用失败:{first}——请检查参数,补齐必填字段后重试。",
    )


def _tools_node_factory(all_tools):
    """Wrap the prebuilt ToolNode with output truncation (plan E2).

    Oversized tool results are middle-truncated (head+tail kept) BEFORE they
    enter the message history, so one huge read_file/bash can't bloat every
    subsequent request. The persisted history and the model view stay
    identical — truncation is part of the record, marked explicitly.

    ``handle_tool_errors=True``: langgraph's DEFAULT handler only converts
    ToolInvocationError and re-raises everything else — a tool that raises
    (e.g. an OSError from a bad path) killed the entire turn as a 500 (the
    2026-08 skill-install incident). With True, ANY exception becomes an
    error ToolMessage the agent can read and recover from. Builtin tools
    already never raise; this is the safety net for MCP/third-party tools.
    """
    node = ToolNode(all_tools, handle_tool_errors=_tool_error_reply)

    async def tools_node(state: AgentState, config=None) -> dict:
        # 工具心跳（2026-10-13 stall 误杀修复·方案 A）：工具执行期间图流完全
        # 安静——messages 只在 LLM 流式时吐字、updates 只在节点完成时发——
        # CHUNK_TIMEOUT_S 看门狗（engine.chunked_stream）会在任何 ≥180s 的
        # 工具跑到一半时误判「模型流断流」（监控 subagent 的 sleep 300 每轮
        # 必死，实测 ae01d586 / 2baf4300）。节点执行期间每 TOOL_HEARTBEAT_S
        # 秒经 get_stream_writer 发一个 custom 心跳，engine 的 stream_mode
        # 已含 "custom"：chunked_stream 对任何 chunk 重置时钟；真断流（无
        # LLM 输出且无工具在跑）照杀不误。writer 不可用（旧 langgraph/非流
        # 式调用）时退化为无心跳，行为同旧版。
        heartbeat: asyncio.Task | None = None
        try:
            writer = None
            try:
                from langgraph.config import get_stream_writer

                writer = get_stream_writer()
            except Exception:  # noqa: BLE001
                writer = None
            if writer is not None:
                async def _beat() -> None:
                    while True:
                        await asyncio.sleep(TOOL_HEARTBEAT_S)
                        try:
                            writer({"ginno_tool_heartbeat": True, "ts": time.time()})
                        except Exception:  # noqa: BLE001
                            return  # 通道关闭：心跳退役，不拖垮工具节点

                heartbeat = asyncio.get_running_loop().create_task(_beat())
            out = await node.ainvoke(state, config)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
        max_chars = int(context_settings().get("tool_output_max_chars", 20000))
        msgs = []
        activated: list[str] = []
        pending = state.get("pending_tool_calls") or []
        pending_by_id = {tc.get("id"): tc for tc in pending if tc.get("id")}
        for m in (out or {}).get("messages", []):
            if isinstance(m, ToolMessage):
                content = truncate_tool_content(getattr(m, "content", ""), max_chars)
                msgs.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=getattr(m, "tool_call_id", None),
                        name=getattr(m, "name", None),
                        id=getattr(m, "id", None),
                        status=getattr(m, "status", None),
                    )
                )
                # A successful use_skill widens this turn's allowlist to the
                # skill's declared tools so the next agent step can actually
                # run them (analyst + aliyun-bill → bash, etc.).
                if getattr(m, "name", None) == "use_skill":
                    raw = str(getattr(m, "content", "") or "")
                    if not raw.startswith("[error]"):
                        tc = pending_by_id.get(getattr(m, "tool_call_id", None)) or {}
                        skill_name = (tc.get("args") or {}).get("name")
                        if skill_name:
                            activated.append(str(skill_name))
            else:
                msgs.append(m)
        update: dict = {"messages": msgs}
        if activated:
            existing = list(state.get("active_skills") or [])
            for n in activated:
                if n not in existing:
                    existing.append(n)
            update["active_skills"] = existing
            # Sticky lazy activation (generalized): a skill declaring
            # `sticky: true` in its SKILL.md also lands on the session-
            # persistent sticky_skills channel — its `tools:` family (lazily
            # gated in tool_allowed) stays bound for the rest of the session
            # after ONE activation. The channel is APPEND-only (operator.add),
            # so the reducer accumulates; send just the new names.
            from .skills.loader import SkillLoader as _Loader

            loader = _Loader(project_slug=state.get("project_slug") or None)
            sticky_now = [n for n in activated if (sk := loader.get(n)) and sk.sticky]
            if sticky_now:
                update["sticky_skills"] = sticky_now
        return update

    return tools_node


def build_graph(
    model,
    project_slug: str,
    workspace: str,
    mcp_tools: list | None = None,
    hook_dispatcher=None,
    all_tools: list | None = None,
    context_dirs: list[dict] | None = None,
    primary_path: str | None = None,
    model_getter=None,
    router=None,
    tool_selector=None,
):
    """Compose the main agent graph (single graph, union toolset).

    Callers may pass a pre-built ``all_tools`` list so the session can keep
    the exact tool names for WorldState sections (mcp/agent snapshots).
    ``model_getter``（模型热切）：agent 节点每次 LLM 调用前调用它取当前模
    型；缺省退化为静态 ``model``。会话构建方应传
    ``server_shared.make_session_model_getter(session_id, model)``，让运行中
    的 turn 也能跟随 PATCH 换模型（model-assignment-design.md §3.2）。
    ``router`` / ``tool_selector``：classifier 中间件（classifier 包），
    由 ``classifier.build_middlewares(session_id)`` 构造；mode=off 时为
    (None, None)，graph 行为与从前逐字节一致。
    """
    if all_tools is None:
        all_tools = build_all_tools(
            mcp_tools,
            workspace=workspace,
            project_slug=project_slug,
            context_dirs=context_dirs,
            primary_path=primary_path,
        )
    policy = PermissionPolicy.from_settings()

    resolve_model = model_getter or (lambda: model)
    g = StateGraph(AgentState)
    g.add_node(
        "agent",
        agent_node_factory(resolve_model, all_tools, router, tool_selector),
    )
    g.add_node("permission", permission_node_factory(policy, hook_dispatcher, all_tools))
    g.add_node("tools", _tools_node_factory(all_tools))

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {"permission": "permission", END: END})
    g.add_edge("permission", "tools")
    g.add_edge("tools", "agent")

    # NOTE: pending_writes intentionally stay hidden from langgraph here (the
    # checkpointer's default) — surfacing them changes resume semantics and,
    # measured, does NOT help the reconnect path anyway: an interrupt raised
    # inside the tools node gets one extra write-less checkpoint committed on
    # top of it, so get_tuple's newest entry carries no `__interrupt__` either
    # way. The reconnect path reads `get_pending_interrupt` instead.
    return g.compile(checkpointer=FileCheckpointer(project_slug=project_slug))
