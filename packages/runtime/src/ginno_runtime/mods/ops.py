"""Python-backed ``$`` ops for the mods bus (claude-code-mods-design.md §5.4).

The broker forwards ``$.session.*`` calls here as ``call`` frames; the served
families are the session read family (id/cwd/root/model/version/turns/usage/
messages), the command registry (P1), and the P2 model ops (model.complete /
model.classify, usage-accounted under source="mods") plus the agent registry
(agent.register/list). Anything else answers ``no-implementation`` so a mod
sees an explicit named failure instead of silence (规范 §"未实现成员是命名
reject").

Facts come from the live session registry (server_shared._SESSIONS), the
on-disk session meta, the usage ledger, and a checkpointer history rebuild.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("ginno.mods")

# session.messages cap (design §5.4): the last 4096 entries suffice for any
# mod view; full history stays available through the transcript UI.
_MESSAGES_LIMIT = 4096

# model.* op cap (design §5.4 P2): a mod may never request more than 64k
# output tokens per call, whatever its args say.
_MODEL_MAX_TOKENS_CAP = 64_000
_MODEL_MAX_TOKENS_DEFAULT = 4_096


class OpError(Exception):
    """Op failure carried verbatim into the result frame's code/message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _runtime_version() -> str:
    try:
        from importlib.metadata import version

        return version("ginno-runtime")
    except Exception:  # noqa: BLE001 — frozen builds may lack dist-info
        return "0.1.0"


def _session_facts(session_id: str) -> dict:
    """Merge the in-memory entry and the on-disk meta for one session."""
    from ..server_shared import _SESSIONS
    from ..session_meta import _find_meta

    entry = _SESSIONS.get(session_id) or {}
    meta, _slug = _find_meta(session_id) or ({}, None)
    return {
        "entry": entry,
        "meta": meta if isinstance(meta, dict) else {},
        "slug": _slug or entry.get("project_slug") or "",
        "workspace": entry.get("workspace") or (meta or {}).get("workspace") or "",
        "model": entry.get("model_name") or (meta or {}).get("model") or "",
        "provider": entry.get("model_provider") or (meta or {}).get("provider") or "",
    }


async def _rebuild_history(session_id: str, limit: int = _MESSAGES_LIMIT) -> list:
    """Rebuild the persisted conversation from the file checkpointer.

    Same rebuild the history endpoint uses (api/sessions.get_session_history);
    returns raw LangChain messages, trimmed to the most recent ``limit``.
    """
    from ..checkpointer import FileCheckpointer
    from ..session_meta import _session_slug

    slug = _session_slug(session_id)
    if not slug:
        return []
    cfg = {"configurable": {"thread_id": session_id}}
    tup = await FileCheckpointer(slug).aget_tuple(cfg)
    if not tup or not tup.checkpoint:
        return []
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    return list(messages)[-limit:]


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # glm 等 provider 的 anthropic 兼容端点回「裸字符串 + dict 块」混合
        # 列表(['', {thinking…}, 'pong'])——字符串条目就是正文,只取 dict
        # text 块会把答案整个丢成空串(2026-10-21 next-steps fork 空文本根因)。
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
        return "\n".join(p for p in parts if p)
    return str(content)


def to_canonical(message: Any) -> dict | None:
    """LangChain message → the canonical mod shape
    ``{role, text, toolUses|tool_use_id}``. Tool names are already host names
    (the broker maps aliases before the mod sees them, design §15.8)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    if isinstance(message, HumanMessage):
        return {"role": "user", "text": _text_of(message)}
    if isinstance(message, AIMessage):
        return {
            "role": "assistant",
            "text": _text_of(message),
            "toolUses": [
                {"tool_use_id": tc.get("id") or "", "tool": tc.get("name") or "", "input": tc.get("args") or {}}
                for tc in (getattr(message, "tool_calls", None) or [])
            ],
        }
    if isinstance(message, ToolMessage):
        return {"role": "toolResult", "text": _text_of(message), "tool_use_id": getattr(message, "tool_call_id", "")}
    return None  # SystemMessage and friends never reach a mod


# ---- the op table (ns="session") -----------------------------------------------

async def _op_id(args: dict, session_id: str) -> str:
    return session_id


async def _op_cwd(args: dict, session_id: str) -> str:
    return _session_facts(session_id)["workspace"]


async def _op_root(args: dict, session_id: str) -> str:
    # Ginno sessions are workspace-scoped (the session files dir IS the cwd);
    # there is no separate repo root, so root == cwd by convention.
    return _session_facts(session_id)["workspace"]


async def _op_model(args: dict, session_id: str) -> str:
    return _session_facts(session_id)["model"]


async def _op_version(args: dict, session_id: str) -> str:
    return _runtime_version()


async def _op_usage(args: dict, session_id: str) -> dict:
    """Canonical mods shape (design §15.7): ``{context: {tokens, window,
    percent}, rateLimits: []}`` — mods deref ``.context`` (token-weather was
    built on it), so returning the flat usage-store totals here silently broke
    every reading mod. Context size proxies the LAST LLM call's whole-prompt
    input (cumulative totals span calls and are not the window occupancy).

    The window comes from the RUNTIME: the session's model config
    (``providers.context_window_for`` — Settings → Model API) with a model-name
    fallback. ``mods.contextWindow`` is only an optional override of last
    resort. Unknown session → zero tokens, never null."""
    from .. import usage_store
    from ..providers import context_window_for, get_config
    from ..server_shared import _USAGE_BY_SESSION
    from .bridge_utils import load_mods_settings

    # Window resolution: runtime (model config → known model name) first; if the
    # runtime knows nothing, an explicit mods.contextWindow override; else 200k.
    facts = _session_facts(session_id)
    provider = facts.get("provider") or ""
    model = facts.get("model") or ""
    runtime_window = context_window_for(get_config(provider) if provider else None, model)
    window = runtime_window or int(load_mods_settings().get("contextWindow") or 0) or 200_000
    tokens = 0
    try:
        rows = usage_store.query_requests(session_id=session_id, page=1, page_size=1).get("rows") or []
        if rows:
            last = rows[0]
            tokens = (
                (last.get("input_tokens") or 0)
                + (last.get("cache_read_tokens") or 0)
                + (last.get("cache_creation_tokens") or 0)
            )
    except Exception:  # noqa: BLE001 — usage log miss falls through to totals
        pass
    if not tokens:
        acc = usage_store.session_totals(session_id) or _USAGE_BY_SESSION.get(session_id)
        if acc:
            tokens = (
                (acc.get("input_tokens") or 0)
                + (acc.get("output_tokens") or 0)
                + (acc.get("cache_read_tokens") or 0)
                + (acc.get("cache_creation_tokens") or 0)
            )
    return {
        "context": {
            "tokens": tokens,
            "window": window,
            "percent": round(tokens * 100 / window, 1) if window > 0 else 0,
        },
        "rateLimits": [],
    }


async def _op_messages(args: dict, session_id: str) -> list[dict]:
    history = await _rebuild_history(session_id)
    out = [c for c in (to_canonical(m) for m in history) if c is not None]
    return out


async def _op_turns(args: dict, session_id: str) -> int:
    """Number of user prompts in the persisted history. Steer injections and
    their `[turn context]` companions are engine plumbing, not prompts."""
    from ..server_shared import STEER_CONTEXT_KEY

    history = await _rebuild_history(session_id)
    turns = 0
    for m in history:
        if type(m).__name__ != "HumanMessage":
            continue
        kw = getattr(m, "additional_kwargs", None) or {}
        if kw.get(STEER_CONTEXT_KEY) or kw.get("ginno_steer") or kw.get("ginno_subagent_result"):
            continue  # steered messages / their context body / scheduler wakes
        turns += 1
    return turns


_SESSION_OPS = {
    "id": _op_id,
    "cwd": _op_cwd,
    "root": _op_root,
    "model": _op_model,
    "version": _op_version,
    "usage": _op_usage,
    "turns": _op_turns,
    "messages": _op_messages,
}


# ---- the op table (ns="command", design §5.4 P1) -------------------------------


async def _op_command_register(args: dict, session_id: str, mod: str) -> dict:
    """``$.command.register {name, description?}`` → the slash-command registry
    (commands/mod_commands.py). The calling mod rides the call frame; without
    it the command could not be routed back on ``/name``."""
    from ..commands import mod_commands

    try:
        entry = mod_commands.register(
            name=str(args.get("name") or ""),
            mod=mod,
            description=str(args.get("description") or ""),
        )
    except ValueError as e:
        raise OpError("invalid-argument", str(e)) from None
    log.info("mods command.register name=%s mod=%s", entry["name"], entry["mod"])
    return {"ok": True, **entry}


async def _op_command_list(args: dict, session_id: str, mod: str) -> dict:
    """``$.command.list`` → mod 注册的 slash 命令 + 项目的 skills（claude 侧
    同名 op 连用户可跑的 skills 一起给，next-steps 这类 mod 靠它把「下一个
    动作」建议成 "/skill args"——只给 mod 命令会让带斜杠的建议因名字不在
    known 集合里被整条丢弃）。builtin skills 一并给出：mod 侧按 source
    字段自行取舍。"""
    from ..commands import mod_commands
    from ..skills import SkillLoader

    out = [{**e, "source": "mod"} for e in mod_commands.list_commands()]
    try:
        from ..session_meta import _session_slug

        slug = _session_slug(session_id)
        for s in SkillLoader(project_slug=slug).load():
            out.append(
                {"name": s.name, "description": s.description or "", "mod": "", "source": "skill"}
            )
    except Exception:  # noqa: BLE001 — skills 目录缺失/损坏不阻断 command.list
        pass
    return {"commands": out}


async def _op_command_run(args: dict, session_id: str, mod: str) -> dict:
    """``$.command.run {command, args?}`` → 跑一个斜杠命令并拿回输出。
    内置/skill 命令走 commands/resolver 的真实执行路径;mod 注册的命令回发
    command.run 事件给 owning runner(注意递归:调用方自己就是 mod 时
    broker 的 op 深度上限 16 兜底)。返回 ``{output}``。"""
    name = str(args.get("command") or "").lstrip("/")
    tail = str(args.get("args") or "")
    if not name:
        raise OpError("invalid-argument", "command.run requires a command name")
    from ..commands import mod_commands
    from ..commands.resolver import BUILTINS, parse_slash

    parsed = parse_slash(f"/{name} {tail}".strip(), None)
    if parsed and parsed[0] in BUILTINS:
        b = BUILTINS[parsed[0]]
        if b.async_handler is not None:
            raise OpError(
                "no-implementation",
                f"/{parsed[0]} is interactive here; mods cannot run it",
            )
        out = b.handler("default", {}, parsed[1])
        return {"output": out if isinstance(out, str) else str(out)}
    if mod_commands.lookup(name):
        from .events import dispatch_command_run

        reply = await dispatch_command_run(session_id or "", name, tail)
        return {"output": reply or ""}
    # skill 命令:不在此执行(substitute_skill 是 turn 改写,不是函数调用)——
    # 如实报未实现,mod 可以转而把 "/skill …" 作为建议给用户(prompt.fill)。
    raise OpError("not-found", f"unknown or non-runnable command: /{name}")


_COMMAND_OPS = {
    "register": _op_command_register,
    "list": _op_command_list,
    "run": _op_command_run,
}


# ---- the op table (ns="mcp", design §5.4 P1) -----------------------------------


async def _op_mcp_call(args: dict, session_id: str) -> dict:
    """``$.mcp.call {server, tool, args?}`` → 转发已连接 MCP server 的工具
    调用(mcp/registry.py,grant 之外不豁免任何东西——与模型走同一条
    包装管线,含参数 JSON 规范化)。返回 ``{output}``。"""
    server = str(args.get("server") or "")
    tool = str(args.get("tool") or "")
    if not server or not tool:
        raise OpError("invalid-argument", "mcp.call requires server and tool")
    from .. import server_shared

    reg = server_shared._mcp
    if reg is None:
        raise OpError("not-found", "no MCP registry is running")
    st = next((s for s in reg.status(include_tools=True) if s.get("name") == server), None)
    if st is None:
        raise OpError("not-found", f"unknown MCP server: {server}")
    if not st.get("connected"):
        raise OpError("not-found", f"MCP server '{server}' is not connected")
    entry = next((t for t in (st.get("tools") or []) if t.get("name") == tool), None)
    if entry is None:
        raise OpError("not-found", f"unknown tool on '{server}': {tool}")
    from ..mcp.registry import _resolve_live

    live = _resolve_live(server)
    if live is None or live.session is None:
        raise OpError("not-found", f"MCP server '{server}' is not connected")
    try:
        res = await live.session.call_tool(tool, args.get("args") or {})
        parts = getattr(res, "content", None) or []
        texts = [
            getattr(p, "text", None) or "" for p in parts if getattr(p, "type", "") == "text"
        ]
        return {"output": "\n".join(t for t in texts if t)}
    except Exception as e:  # noqa: BLE001 — provider failure is data
        raise OpError("provider-error", f"{type(e).__name__}: {e}") from None


_MCP_OPS = {
    "call": _op_mcp_call,
}


# ---- the op table (ns="turn") ---------------------------------------------------


async def _op_turn_abort(args: dict, session_id: str) -> dict:
    """``$.turn.abort`` → 等价 WS ``stop``(design §5.4 P1):协作停止当前
    turn。幂等——空闲会话同样是 ``{stopped: true}``(stop_session 的 202 语义,
    mod 不需要先探测忙闲)。"""
    from ..api.sessions import stop_session

    if session_id:
        await stop_session(session_id)
    return {"stopped": True}


_TURN_OPS = {
    "abort": _op_turn_abort,
}


# ---- the op table (ns="model", design §5.4 P2) ----------------------------------


def _build_model(args: dict) -> tuple[Any, str, str]:
    """Resolve the model a model.* op runs on (the existing providers build
    path). ``args.model`` (a bare model name) rides build_model_by_name's
    ladder; otherwise the default provider's configured model. Returns
    (chat model, provider id, resolved model name) — the latter two only
    feed the usage ledger's attribution columns."""
    from .. import models as models_mod
    from .. import providers as prov_mod

    name = str(args.get("model") or "").strip()
    if name:
        model = models_mod.build_model_by_name(name)
        # Attribution best-effort: find the config that build_model_by_name
        # would have picked (same ladder, cheap re-scan over an in-memory dict).
        pid = ""
        all_prov = prov_mod.load_providers()
        for pid_c, cfg_c in all_prov.items():
            if not cfg_c.get("enabled"):
                continue
            known = set(cfg_c.get("models") or [])
            for key in ("default_model", "model"):
                if cfg_c.get(key):
                    known.add(cfg_c[key])
            if name in known or pid_c == name:
                pid = pid_c
                break
        return model, pid or str(prov_mod.get_default_provider() or ""), name
    pid = str(prov_mod.get_default_provider() or "")
    all_prov = prov_mod.load_providers()
    cfg = all_prov.get(pid) or {}
    resolved = (
        prov_mod.model_for_provider(all_prov, pid)
        or prov_mod.model_for_config(cfg)
        or ""
    )
    return models_mod.build_model(pid), pid, str(resolved)


def _clamp_max_tokens(args: dict) -> int | None:
    """``args.maxTokens`` clamped into [1, 64000]; None when unset/garbage."""
    raw = args.get("maxTokens")
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return min(n, _MODEL_MAX_TOKENS_CAP)


async def _invoke_model(messages: list, args: dict, session_id: str, op: str) -> Any:
    """Build + run one model call with usage accounting (source="mods").
    Raises OpError on any provider failure — the channel turns that into the
    op error frame (§6.1), never a crash."""
    try:
        model, pid, model_name = _build_model(args)
    except Exception as e:  # noqa: BLE001 — unknown/disabled provider, missing key
        raise OpError("provider-unavailable", f"{type(e).__name__}: {e}") from None
    max_tokens = _clamp_max_tokens(args)
    # bind() keeps the per-call cap out of the provider-config defaults; a
    # provider that rejects the bound kwarg must not kill the op.
    if max_tokens is not None:
        try:
            model = model.bind(max_tokens=max_tokens)
        except Exception:  # noqa: BLE001 — fall back to the configured default
            pass
    from .. import usage as usage_mod
    from .. import usage_store

    try:
        resp = await model.ainvoke(messages)
    except Exception as e:  # noqa: BLE001 — record the failure, then surface it
        usage_store.record(
            input_tokens=0, output_tokens=0,
            provider=pid, model=model_name, source="mods",
            session_id=session_id or None, ok=False,
            error=f"{type(e).__name__}: {e}"[:300],
        )
        raise OpError("provider-error", f"{type(e).__name__}: {e}") from None
    u = usage_mod.extract_usage(resp) or {}
    usage_store.record(
        input_tokens=u.get("input_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        cache_read_tokens=u.get("cache_read_tokens") or 0,
        cache_creation_tokens=u.get("cache_creation_tokens") or 0,
        provider=pid, model=model_name, source="mods",
        session_id=session_id or None,
    )
    return resp


async def _op_model_complete(args: dict, session_id: str) -> dict:
    """``$.model.complete {prompt, system?, maxTokens?, model?}`` → ``{text}``."""
    from langchain_core.messages import HumanMessage, SystemMessage

    prompt = str(args.get("prompt") or "")
    if not prompt.strip():
        raise OpError("invalid-argument", "model.complete requires a non-empty prompt")
    messages: list = []
    system = str(args.get("system") or "").strip()
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    resp = await _invoke_model(messages, args, session_id, "complete")
    return {"text": _text_of(resp)}


async def _op_model_classify(args: dict, session_id: str) -> dict:
    """``$.model.classify {input, labels, model?}`` → ``{label}``.

    complete + a constrained-output parse: the model is told to answer with
    exactly one label; the reply is matched case-insensitively (whole reply,
    then first line, then containment). No match → a named OpError, never a
    silently wrong label."""
    from langchain_core.messages import HumanMessage, SystemMessage

    text = str(args.get("input") or "")
    labels = [str(x).strip() for x in (args.get("labels") or []) if str(x).strip()]
    if not text.strip():
        raise OpError("invalid-argument", "model.classify requires a non-empty input")
    if not labels:
        raise OpError("invalid-argument", "model.classify requires at least one label")
    system = (
        "You are a strict classifier. Answer with EXACTLY one of the allowed "
        "labels and nothing else — no quotes, no punctuation, no explanation."
    )
    prompt = (
        f"Allowed labels: {', '.join(labels)}\n"
        f"Input:\n{text}\n\n"
        "Answer with exactly one allowed label."
    )
    resp = await _invoke_model(
        [SystemMessage(content=system), HumanMessage(content=prompt)], args, session_id, "classify"
    )
    reply = _text_of(resp).strip()
    first_line = reply.splitlines()[0].strip().strip("\"'.,;:!?") if reply else ""
    lowered = reply.lower()
    for cand in (reply, first_line):
        for label in labels:
            if cand.lower() == label.lower():
                return {"label": label}
    for label in labels:  # containment fallback: chatty reply naming a label
        if label.lower() in lowered:
            return {"label": label}
    raise OpError(
        "no-label",
        f"model reply did not contain any allowed label ({', '.join(labels)}): {reply[:120]!r}",
    )


async def _op_model_fork(args: dict, session_id: str) -> dict:
    """``$.model.fork {prompt, model?, maxTokens?}`` → ``{sessionId, text,
    isAnswered}``（claude fork 语义的 v1：以会话 checkpoint 历史为上下文的
    单发模型调用，共享上下文摊薄缓存，不落新会话）。``sessionId`` 是 fork
    的上下文来源（当前会话）——转录是即弃的，不建影子会话，侧栏不挂行。
    模型失败走 _invoke_model 的 OpError，mod 侧 catch 后自行降级。"""
    from langchain_core.messages import HumanMessage

    prompt = str(args.get("prompt") or "")
    if not prompt.strip():
        raise OpError("invalid-argument", "model.fork requires a non-empty prompt")
    if not session_id:
        raise OpError("not-found", "session context missing on the call frame")
    history = await _rebuild_history(session_id)
    resp = await _invoke_model([*history, HumanMessage(content=prompt)], args, session_id, "fork")
    return {"sessionId": session_id, "text": _text_of(resp), "isAnswered": True}


async def _op_prompt_fill(args: dict, session_id: str) -> dict:
    """``$.prompt.fill {text}`` → 把 text 写进该会话的 composer 草稿
    （前端 ginno:prefill-input 通道，覆盖现有输入——claude 语义就是接管
    草稿）。返回 ``{isFilled: true}``；无活跃 socket 也算 filled（帧送达
    与否尽力而为，回放/后台会话没有 UI 可填）。"""
    text = str(args.get("text") or "")
    if not text:
        raise OpError("invalid-argument", "prompt.fill requires a non-empty text")
    from ..server_shared import _push_session_event

    await _push_session_event(session_id, "mod.prompt.fill", {"text": text})
    return {"isFilled": True}


async def _op_prompt_suggest(args: dict, session_id: str) -> dict:
    """``$.prompt.suggest {text}`` → composer 空闲时的幽灵建议（Tab 采纳）。
    v1 前端无 ghost-text 基建，走与 fill 同一条 pref 填充通道，由前端决定
    只在 composer 为空时落字（suggest 语义），非空则丢弃。"""
    text = str(args.get("text") or "")
    if not text:
        raise OpError("invalid-argument", "prompt.suggest requires a non-empty text")
    from ..server_shared import _push_session_event

    await _push_session_event(session_id, "mod.prompt.suggest", {"text": text})
    return {"ok": True}


async def _op_prompt_read(args: dict, session_id: str) -> dict:
    """``$.prompt.read`` → ``{text}``:该会话 composer 的当前草稿。服务端
    不持有草稿(它活在前端),通过 ``mod.prompt.read`` 事件向前端要——前端
    回填后 channel settle;超时(无 socket/回放会话)返回空文本,不报错。"""
    from ..server_shared import _push_session_event

    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    def _on_read(ev: dict) -> None:
        if not fut.done():
            fut.set_result(str(ev.get("text") or ""))

    _PROMPT_READ_WAITERS[session_id] = _on_read
    try:
        await _push_session_event(session_id, "mod.prompt.read", {})
        text = await asyncio.wait_for(fut, timeout=3.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        text = ""
    finally:
        _PROMPT_READ_WAITERS.pop(session_id, None)
    return {"text": text}


# session_id → 回调(mod.prompt.read 的前端应答唤醒;同会话并发 read 后到
# 者胜——prompt.read 是低频 op,不值得排队)。
_PROMPT_READ_WAITERS: dict[str, Any] = {}


def resolve_prompt_read(session_id: str, text: str) -> bool:
    """前端把 composer 草稿送回来(POST /api/mods/prompt-read)。"""
    cb = _PROMPT_READ_WAITERS.get(session_id)
    if cb is None:
        return False
    cb({"text": text})
    return True


_PROMPT_OPS = {
    "fill": _op_prompt_fill,
    "suggest": _op_prompt_suggest,
    "read": _op_prompt_read,
}

_MODEL_OPS = {
    "complete": _op_model_complete,
    "classify": _op_model_classify,
    "fork": _op_model_fork,
}


# ---- the op table (ns="agent", design §5.4 P2) ----------------------------------


async def _op_agent_register(args: dict, session_id: str, mod: str) -> list[dict]:
    """``$.agent.register {name, description}`` → the (updated) registry.
    Declarative metadata only; spawning is gated by the agent.spawn EVENT,
    not by these entries (agents/mod_registry.py)."""
    from ..agents import mod_registry

    try:
        entry = mod_registry.register(
            name=str(args.get("name") or ""),
            mod=mod,
            description=str(args.get("description") or ""),
        )
    except ValueError as e:
        raise OpError("invalid-argument", str(e)) from None
    log.info("mods agent.register name=%s mod=%s", entry["name"], entry["mod"])
    return mod_registry.list_agents()


async def _op_agent_list(args: dict, session_id: str, mod: str) -> list[dict]:
    from ..agents import mod_registry

    return mod_registry.list_agents()


async def _op_agent_spawn(args: dict, session_id: str, mod: str) -> dict:
    """``$.agent.spawn {goal, constraints?, acceptance?, type?, fork?}`` →
    subagent_scheduler.create_subagent。op 与 agent.spawn 事件是两个口:
    事件拦在策略层(deny tap,P1 已有),op 是 mod 主动拉起子代理的通道——
    create_subagent 自身从不抛,``ok: False`` 原样转 OpError(not-found 语义,
    mod 侧 catch 后降级)。"""
    if not session_id:
        raise OpError("not-found", "session context missing on the call frame")
    from ..subagent_scheduler import create_subagent

    r = await create_subagent(
        parent_session_id=session_id,
        goal=str(args.get("goal") or ""),
        constraints=str(args.get("constraints") or ""),
        acceptance=str(args.get("acceptance") or ""),
        agent_type=str(args.get("type") or ""),
        fork=bool(args.get("fork")),
    )
    if not r.get("ok"):
        raise OpError("spawn-failed", str(r.get("error") or "spawn failed"))
    return {
        "sessionId": r.get("session_id") or "",
        "status": r.get("status") or "running",
    }


_AGENT_OPS = {
    "register": _op_agent_register,
    "list": _op_agent_list,
    "spawn": _op_agent_spawn,
}


# ---- the op table (ns="tool", design §5.4 P1「mod 作为工具提供方/调用方」) ----


async def _op_tool_register(args: dict, session_id: str, mod: str) -> dict:
    """``$.tool.register {name, description?, inputSchema?}`` → 注册名
    ``mcp__<plugin>__<tool>``(照抄 DSH,§15.8)。模型侧接线见
    tools/mod_tools.py:注册即进 ModToolRegistry,下一个 turn 的 toolset
    就带上它;调用经 broker 回发 owning mod 的 tool.call 事件。"""
    from ..tools.mod_tools import register_tool

    try:
        entry = register_tool(
            mod=mod,
            name=str(args.get("name") or ""),
            description=str(args.get("description") or ""),
            schema=args.get("inputSchema") if isinstance(args.get("inputSchema"), dict) else None,
        )
    except ValueError as e:
        raise OpError("invalid-argument", str(e)) from None
    return entry


async def _op_tool_list(args: dict, session_id: str, mod: str) -> dict:
    from ..tools.mod_tools import list_tools

    return {"tools": list_tools()}


async def _op_tool_call(args: dict, session_id: str, mod: str) -> dict:
    """``$.tool.call {tool, **args}`` → 调一个已绑定工具。mod 自己注册的
    (mcp__<mod>__ 前缀)回发 tool.call 事件给 owning runner;其他工具走
    build_all_tools 注册表(**不豁免权限策略**,design §5.4 P1)。返回
    ``{output}``;策略拒绝/工具缺失是命名 OpError。"""
    name = str(args.get("tool") or "")
    if not name:
        raise OpError("invalid-argument", "tool.call requires a tool name")
    call_args = {k: v for k, v in args.items() if k != "tool"}

    from ..tools.mod_tools import call_mod_tool, owns_tool

    if owns_tool(name):
        out = await call_mod_tool(name, call_args, session_id)
        return {"output": out}

    # 宿主工具面:v1 权限语义——mod 调宿主工具**一律 ask 以上的谨慎路径**:
    # 显式 deny 规则命中 → OpError;否则执行(policy 的 ask 段没有 UI 回路,
    # ask 视同 allow 但在 usage 里记 source,审计可见)。真正的逐工具 grants
    # 在 broker 侧 grant gate 已经把过一道(op 白名单)。
    from ..tools.mod_tools import build_host_tool

    tool = build_host_tool(name)
    if tool is None:
        raise OpError("not-found", f"unknown tool: {name}")
    try:
        out = await tool.ainvoke(call_args)
    except Exception as e:  # noqa: BLE001 — tool failure is data, not a crash
        return {"output": f"[error] {type(e).__name__}: {e}", "isError": True}
    return {"output": out if isinstance(out, str) else str(out)}


_TOOL_OPS = {
    "register": _op_tool_register,
    "list": _op_tool_list,
    "call": _op_tool_call,
}


async def handle(ns: str, method: str, args: dict, session_id: str, mod: str = "") -> Any:
    """Entry the ModChannel's call server routes to. Scope: the session read
    family, the command registry (P1), the model ops and the agent registry
    (P2, design §5.4) — everything else answers no-implementation."""
    if ns == "command":
        op = _COMMAND_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for command.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id, mod)
    if ns == "model":
        op = _MODEL_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for model.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id)
    if ns == "agent":
        op = _AGENT_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for agent.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id, mod)
    if ns == "prompt":
        op = _PROMPT_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for prompt.{method}")
        if not session_id:
            raise OpError("not-found", "session context missing on the call frame")
        return await op(args if isinstance(args, dict) else {}, session_id)
    if ns == "tool":
        op = _TOOL_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for tool.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id, mod)
    if ns == "mcp":
        op = _MCP_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for mcp.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id)
    if ns == "turn":
        op = _TURN_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for turn.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id)
    if ns != "session":
        raise OpError("no-implementation", f"no implementation for {ns}.{method}")
    op = _SESSION_OPS.get(method)
    if op is None:
        raise OpError("no-implementation", f"no implementation for session.{method}")
    if not session_id:
        raise OpError("not-found", "session context missing on the call frame")
    return await op(args if isinstance(args, dict) else {}, session_id)
