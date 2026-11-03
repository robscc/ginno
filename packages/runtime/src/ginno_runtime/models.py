"""Model factory — build a LangChain chat model from a provider id.

Reads provider config from settings.json `providers` (see providers.py).
Sampling params: temperature + max_tokens as explicit top-level kwargs —
langchain warns ("should be specified explicitly") whenever max_tokens rides
inside model_kwargs. base_url + api_key as top-level kwargs (verified present
on ChatAnthropic + ChatOpenAI).
"""

from __future__ import annotations

import os
from typing import Any

from . import providers as prov_mod
from ._version import ginno_user_agent

# Per-request timeout (seconds) for chat calls. Without this the SDK falls back
# to its ~600s default and a stalled gateway makes a turn hang for minutes (the
# 7m49s "stuck at 'now creating doc'" symptom). A finite value fails fast on a
# network stall while still allowing a long, steadily-streaming answer. NOTE:
# ChatAnthropic's pydantic config only accepts a *number* here (not an
# httpx.Timeout object), so we pass seconds. The *generator-level* stall (model
# stuck mid-generation, not a network read) is handled separately by the
# per-chunk stall watchdog in server._stream_graph (CHUNK_TIMEOUT_S).
# Module-level so tests can monkeypatch a short value. P2 contract 7: this is
# now the FALLBACK — the provider config's ``timeout_s`` (default 300, merged
# by normalize_config) overrides it per provider, so long replies are bounded
# by the value the settings UI actually shows.
CHAT_TIMEOUT_S = 180.0


def _chat_timeout(cfg: dict[str, Any] | None = None) -> float:
    """Per-request timeout: the provider config's ``timeout_s`` when set (P2
    contract 7), else the module default. A malformed stored value degrades to
    the default instead of poisoning model construction."""
    try:
        t = float((cfg or {}).get("timeout_s") or 0)
    except (TypeError, ValueError):
        t = 0.0
    return t if t > 0 else CHAT_TIMEOUT_S


# Lazily-built (import-time work stays off the startup path like the rest of
# this module) and cached: one ChatOpenAI subclass shared by every build.
_REASONING_CLS: type | None = None


def _reasoning_chat_openai() -> type:
    """ChatOpenAI subclass that preserves third-party ``reasoning_content``.

    langchain-openai 1.x targets the official OpenAI spec only and silently
    drops non-standard streaming delta fields — notably ``reasoning_content``,
    which Qwen/DashScope compatible-mode, DeepSeek and vLLM gateways use for
    thinking-mode output. Ginno's UI pipeline reads that field from
    ``additional_kwargs`` (api/stream.py → ``thinking.delta`` event;
    api/messages_ui.py replays it into history), so re-attach it after the
    base conversion. Chunk accumulation (AIMessageChunk + AIMessageChunk)
    merges additional_kwargs, so the persisted message carries the full
    reasoning text — same shape the 0.3.x era produced.
    """
    global _REASONING_CLS
    if _REASONING_CLS is not None:
        return _REASONING_CLS

    from langchain_openai import ChatOpenAI

    class ReasoningChatOpenAI(ChatOpenAI):
        # Emit ``reasoning_content`` on the way back out (see
        # _replay_reasoning_content). Default on for openai-compatible
        # gateways, off for the official OpenAI endpoint — build_model decides
        # from the protocol; ``reasoning_passthrough`` in the provider config
        # overrides either way.
        reasoning_passthrough: bool = True

        def _convert_chunk_to_generation_chunk(
            self, chunk, default_chunk_class, base_generation_info
        ):
            gen_chunk = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            if gen_chunk is None:
                return None
            try:
                choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
            except (AttributeError, IndexError, TypeError):
                delta = {}
            rc = delta.get("reasoning_content")
            # Membership, not truthiness: DeepSeek returns
            # ``reasoning_content: ""`` on some tool-call turns and then
            # requires that exact field back on the next request.
            if rc is not None:
                ak = getattr(gen_chunk.message, "additional_kwargs", None)
                if ak is not None:
                    ak["reasoning_content"] = rc
            return gen_chunk

        def _get_request_payload(
            self, input_, *, stop: list[str] | None = None, **kwargs: Any
        ) -> dict:
            messages = _with_interrupted_tool_results(
                self._convert_input(input_).to_messages()
            )
            payload = super()._get_request_payload(messages, stop=stop, **kwargs)
            if self.reasoning_passthrough:
                _replay_reasoning_content(messages, payload)
            return payload

    _REASONING_CLS = ReasoningChatOpenAI
    return _REASONING_CLS


def _with_interrupted_tool_results(messages: list[Any]) -> list[Any]:
    """Make every assistant ``tool_calls`` batch answerable for the provider.

    Two shapes break the OpenAI/Anthropic contract, and both have been seen in
    live sessions:

    * a ``tool_calls`` message with no answers at all — a turn killed between
      its assistant step committing and the tools node running (app quit /
      sidecar rebuild), or a hook block that appended a synthetic assistant
      message instead of a tool result;
    * a tool result that does NOT immediately follow its assistant because two
      assistant tool-call messages were committed back to back
      (``[AI(X), AI(Y), Tool(X)]``, 2026-10-23 session 4e5b9f89). The real
      response then lands after the wrong assistant and the provider reports
      the call as unanswered.

    Repair, on the payload copy only (persisted history is untouched):

    * answers are re-attached directly after their assistant, in call order —
      a healthy history is a no-op;
    * missing answers get the same ``(interrupted)`` placeholders the stop
      heal uses;
    * a ``ToolMessage`` whose call no longer exists anywhere in its turn is
      dropped — a provider rejects it as "not a response to a tool call".

    Segments are split on user/system messages; tool results never cross one,
    so the repair can never move an answer into a different turn.
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    out: list[Any] = []

    def _call_ids(m: Any) -> list[tuple[str, str]]:
        ids: list[tuple[str, str]] = []
        for attr in ("tool_calls", "invalid_tool_calls"):
            for tc in getattr(m, attr, None) or []:
                if isinstance(tc, dict) and tc.get("id"):
                    ids.append((str(tc["id"]), str(tc.get("name") or "")))
        return ids

    def _flush_segment(segment: list[Any]) -> None:
        by_id: dict[str, Any] = {}
        for m in segment:
            if isinstance(m, ToolMessage):
                tid = str(getattr(m, "tool_call_id", None) or "")
                if tid and tid not in by_id:
                    by_id[tid] = m
        claimed: set[str] = set()
        for m in segment:
            if isinstance(m, ToolMessage):
                continue  # re-emitted next to its call, or dropped as orphan
            out.append(m)
            if not isinstance(m, AIMessage):
                continue
            for tid, name in _call_ids(m):
                if tid in claimed:
                    continue
                claimed.add(tid)
                answer = by_id.get(tid)
                if answer is not None:
                    out.append(answer)
                else:
                    out.append(
                        ToolMessage(
                            content="(interrupted)",
                            tool_call_id=tid,
                            name=name or None,
                        )
                    )

    segment: list[Any] = []
    for m in messages:
        if isinstance(m, (HumanMessage, SystemMessage)):
            if segment:
                _flush_segment(segment)
                segment = []
            out.append(m)
        else:
            segment.append(m)
    if segment:
        _flush_segment(segment)
    return out


def _reasoning_text(message: Any) -> str | None:
    """Best-effort ``reasoning_content`` for one assistant message.

    Two shapes reach us:

    * third-party gateways (DeepSeek / Qwen / vLLM) stream it as a delta field,
      which ReasoningChatOpenAI parks in ``additional_kwargs``;
    * Anthropic-protocol thinking arrives as ``{"type": "thinking", ...}``
      content blocks. A session started on a thinking provider and switched to
      DeepSeek mid-flight still holds those blocks — they must be replayed as
      ``reasoning_content`` on the chat-completions wire.
    """
    ak = getattr(message, "additional_kwargs", None) or {}
    if "reasoning_content" in ak:
        rc = ak.get("reasoning_content")
        if isinstance(rc, str):
            return rc
        if isinstance(rc, list):
            parts = [
                str(b.get("text") or "")
                for b in rc
                if isinstance(b, dict)
            ]
            if parts:
                return "".join(parts)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = [
            str(b.get("thinking") or b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") in ("thinking", "reasoning")
        ]
        if parts:
            return "".join(parts)
    return None


def _replay_reasoning_content(messages: list[Any], payload: dict) -> None:
    """Put ``reasoning_content`` back on the assistant messages of a request.

    langchain-openai's ``_convert_message_to_dict`` sends only the official
    OpenAI message shape, so reasoning never round-trips and DeepSeek's
    thinking mode answers 400 ("The `reasoning_content` in the thinking mode
    must be passed back to the API"). Verified against the live API
    (2026-10-23): **every** assistant message that carries ``tool_calls`` must
    replay it, and so must the final assistant message of the request even
    when it has no tool calls (a synthetic tail — a hook-blocked result, an
    injected notice — otherwise 400s). Mid-history content-only turns were
    tolerated, but the documented contract asks for all of them, and an empty
    string is accepted; filling every assistant message keeps the rule simple
    and covers both shapes.

    The wire list is 1:1 with ``messages`` on the chat-completions path; any
    other shape (Responses API, pre-converted input) is left untouched.
    """
    wire = payload.get("messages")
    if not isinstance(wire, list) or len(wire) != len(messages):
        return
    for message, msg_dict in zip(messages, wire):
        if not isinstance(msg_dict, dict) or msg_dict.get("role") != "assistant":
            continue
        rc = _reasoning_text(message)
        if rc is None:
            # Nothing to replay (a turn from a non-thinking provider, or a
            # synthetic assistant message). The field must still be present;
            # DeepSeek accepts an empty string.
            rc = ""
        msg_dict["reasoning_content"] = rc


# Lazily-built Responses-API class shared by every build (same pattern as
# ReasoningChatOpenAI above).
_RESPONSES_CLS: type | None = None


def bridge_responses_reasoning(gen_chunk):
    """Re-attach Responses-API reasoning summaries as ``reasoning_content``.

    The Responses API streams thinking output as official *reasoning summary*
    events, which langchain-openai turns into content blocks shaped
    ``{"type": "reasoning", "summary": [{"type": "summary_text", "text": ...}]}``
    — NOT the third-party ``delta.reasoning_content`` field that
    ReasoningChatOpenAI harvests on the chat-completions path. Ginno's
    thinking pipeline (api/stream.py ``thinking.delta``; api/messages_ui.py
    replay) reads ``additional_kwargs["reasoning_content"]``, so copy each
    delta's summary text there. Chunk accumulation merges additional_kwargs,
    so the persisted message ends up with the full reasoning text.
    """
    try:
        content = gen_chunk.message.content
        if isinstance(content, list):
            texts = [
                s.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "reasoning"
                for s in (block.get("summary") or [])
                if isinstance(s, dict)
            ]
            text = "".join(t for t in texts if t)
            if text:
                ak = getattr(gen_chunk.message, "additional_kwargs", None)
                if ak is not None:
                    ak["reasoning_content"] = text
    except (AttributeError, TypeError):
        pass
    return gen_chunk


def _responses_chat_openai() -> type:
    """ChatOpenAI pinned to the Responses API, with the reasoning bridge.

    ``use_responses_api=True`` sends everything to ``POST /v1/responses``;
    the LangChain message interface is unchanged, so the stream/graph/usage
    pipeline needs zero edits (design doc §3.3 — P2's cheap win). The
    subclass only re-routes reasoning summaries into the field the rest of
    Ginno expects.
    """
    global _RESPONSES_CLS
    if _RESPONSES_CLS is not None:
        return _RESPONSES_CLS

    from langchain_openai import ChatOpenAI

    class ResponsesReasoningChatOpenAI(ChatOpenAI):
        def _get_request_payload(
            self, input_, *, stop: list[str] | None = None, **kwargs: Any
        ) -> dict:
            messages = _with_interrupted_tool_results(
                self._convert_input(input_).to_messages()
            )
            return super()._get_request_payload(messages, stop=stop, **kwargs)

        def _stream_responses(self, messages, stop=None, run_manager=None, **kwargs):
            for gen in super()._stream_responses(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                yield bridge_responses_reasoning(gen)

        async def _astream_responses(self, messages, stop=None, run_manager=None, **kwargs):
            async for gen in super()._astream_responses(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                yield bridge_responses_reasoning(gen)

    _RESPONSES_CLS = ResponsesReasoningChatOpenAI
    return _RESPONSES_CLS

# Lazily-built Anthropic class (same pattern as ReasoningChatOpenAI): the
# provider needs the same dangling-tool_call repair as the OpenAI path.
_ANTHROPIC_CLS: type | None = None


def _healing_chat_anthropic() -> type:
    global _ANTHROPIC_CLS
    if _ANTHROPIC_CLS is not None:
        return _ANTHROPIC_CLS

    from langchain_anthropic import ChatAnthropic

    class HealingChatAnthropic(ChatAnthropic):
        def _get_request_payload(
            self, input_, *, stop: list[str] | None = None, **kwargs: Any
        ) -> dict:
            messages = _with_interrupted_tool_results(
                self._convert_input(input_).to_messages()
            )
            return super()._get_request_payload(messages, stop=stop, **kwargs)

    _ANTHROPIC_CLS = HealingChatAnthropic
    return _ANTHROPIC_CLS


# ---- 思考强度(thinking-effort-design.md §1/§3)-----------------------------
THINKING_EFFORTS = ("off", "low", "medium", "high")

# 档位 → token 预算内置默认;provider 配置 thinking_budgets 可整体覆盖。
_THINKING_BUDGETS = {"low": 4096, "medium": 16384, "high": 32768}


def normalize_effort(value: Any) -> str | None:
    """容错解析档位:None/"auto"/空/非法值 → None(= 跟随 provider 配置)。"""
    v = str(value or "").strip().lower()
    return v if v in THINKING_EFFORTS else None


def thinking_budgets(cfg: dict) -> dict[str, int]:
    """档位 → token 预算;provider 配置 thinking_budgets 的正整数覆盖默认。"""
    out = dict(_THINKING_BUDGETS)
    custom = cfg.get("thinking_budgets")
    if isinstance(custom, dict):
        for k in out:
            try:
                n = int(custom.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out[k] = n
    return out


def thinking_dialect(cfg: dict, model: str | None) -> str:
    """openai-compat 的思考方言:qwen / glm / deepseek / generic(按模型名 +
    base_url 判别;判别不出 → generic 尽力透传 reasoning_effort)。"""
    m = (model or "").lower()
    if m.startswith(("qwen", "qwq")):
        return "qwen"
    if "dashscope" in str(cfg.get("base_url") or "").lower():
        return "qwen"
    if m.startswith(("glm", "chatglm")) or "bigmodel" in str(cfg.get("base_url") or "").lower():
        return "glm"
    if "deepseek" in m:
        return "deepseek"
    return "generic"


def _effective_effort(cfg: dict, value: Any) -> str | None:
    """解析档位并套能力闸门:provider 显式声明 thinking_mode="none" 时按
    auto 处理(UI 置灰之外的服务端兜底,见设计 §7)。"""
    eff = normalize_effort(value)
    if eff and str(cfg.get("thinking_mode") or "auto") == "none":
        return None
    return eff


def _sampling(cfg: dict[str, Any]) -> tuple[float | None, int | None]:
    """Sampling params for the chat constructors (both explicit kwargs).

    max_tokens 语义(2026-10-16,office-bench 根因修复):正值 → int 值;
    0 → None(完全不传,交由端点自决——thinking 模型的 reasoning 计入输出
    预算,一个过小的上限会让重任务在思考阶段耗尽预算、content 一个字不吐);
    缺省(None,只有未 normalize 的调用方)→ 沿用历史默认 8192。
    以显式 ``max_tokens=`` kwarg 传给 ChatX 构造器,不再经 model_kwargs
    (langchain 对后者每次打 UserWarning: should be specified explicitly)。
    """
    temperature = cfg.get("temperature")
    mt = cfg.get("max_tokens")
    max_tokens: int | None
    if mt is None:
        max_tokens = 8192
    elif int(mt) > 0:
        max_tokens = int(mt)
    else:  # 0 → None = 不传,端点自决
        max_tokens = None
    return (float(temperature) if temperature is not None else None, max_tokens)


def server_web_search_on(cfg: dict) -> bool:
    """True when this provider searches the web *itself*, through the
    Anthropic protocol's server-side ``web_search`` tool.

    Gin's own ``web_search`` tool (tools/web_tools.py) scrapes a search engine
    from the machine; that is unreachable on networks that block the public
    engines, and duplicating it makes the model search twice. Gateways that
    implement Anthropic's server tools (corporate model hubs, proxies) run the
    search on their side and return the results inline with the answer, so we
    bind their tool instead and leave ours unbound — see
    graph.build_all_tools.
    """
    return bool(cfg.get("protocol") == "anthropic" and cfg.get("server_web_search"))


# The server-side tool dict, passed through to the API verbatim. langchain-anthropic
# recognises any tool type starting with `web_search_` as builtin (its
# _BUILTIN_TOOL_PREFIXES) and forwards it without conversion, and web_search is GA
# so it needs no beta header. `max_uses` bounds gateway-side search calls per turn.
_SERVER_WEB_SEARCH_TYPE = "web_search_20250305"


def _server_web_search_tool(cfg: dict) -> dict[str, Any]:
    try:
        max_uses = int(cfg.get("server_web_search_max_uses") or 5)
    except (TypeError, ValueError):
        max_uses = 5
    return {
        "type": _SERVER_WEB_SEARCH_TYPE,
        "name": "web_search",
        "max_uses": max(1, min(max_uses, 20)),
    }


def _anthropic_with_server_search(model, cfg: dict):
    """Return *model* with every ``bind_tools`` call carrying the gateway's
    server-side web_search tool.

    The graph binds the per-agent tool allowlist itself
    (graph.agent_node_factory → model.bind_tools(allowed)), so pre-binding here
    would hand back a RunnableBinding with no bind_tools and the allowlist
    would be dropped. Subclassing keeps the normal bind path intact and makes
    the server tool unconditional — it is a model capability, not a tool the
    agent's allowlist can (or should) revoke.
    """
    base = type(model)

    class _WithServerWebSearch(base):
        def bind_tools(self, tools=None, **kwargs):
            merged = list(tools or [])
            if not any(
                isinstance(t, dict)
                and str(t.get("type") or "").startswith("web_search_")
                for t in merged
            ):
                merged.append(_server_web_search_tool(cfg))
            return super().bind_tools(merged, **kwargs)

    # Rebuild the same model as the subclass so pydantic validation still runs
    # (a plain __class__ assignment is rejected by pydantic's __setattr__).
    return _WithServerWebSearch(**cfg_dump_fields(model))


def cfg_dump_fields(model) -> dict[str, Any]:
    """The constructor fields of a pydantic model instance (best effort)."""
    try:
        return {k: getattr(model, k) for k in type(model).model_fields}
    except Exception:
        return {}


def build_model(
    provider_id: str,
    model_name: str | None = None,
    enable_search: bool | None = None,
    session_id: str | None = None,
    thinking_effort: str | None = None,
):
    """Return a LangChain chat model for the given provider id.

    `model_name` overrides the provider's configured default/model.
    ``thinking_effort``("off"/"low"/"medium"/"high";None="auto" 跟随配置)按
    协议映射成 thinking 参数(thinking-effort-design §3)——anthropic 出
    budget_tokens、responses 出 reasoning.effort、compat 按方言。
    Raises ValueError if the provider is unknown, disabled, or missing a key.

    Test seam: when ``GINNO_FAKE_LLM`` is set, return a deterministic
    ScriptedChatModel (script from ``GINNO_FAKE_LLM_SCRIPTS``) instead of a real
    provider. Off by default; has zero effect on the normal production path.
    """
    if os.environ.get("GINNO_FAKE_LLM"):
        import logging

        logging.getLogger(__name__).warning(
            "GINNO_FAKE_LLM is set — using a deterministic ScriptedChatModel "
            "instead of a real provider"
        )
        from .testing.fake_model import build_fake_model

        return build_fake_model()

    all_prov = prov_mod.load_providers()
    cfg = all_prov.get(provider_id)
    if not cfg:
        raise ValueError(f"unknown provider: {provider_id}")
    if not cfg.get("enabled"):
        raise ValueError(f"provider {provider_id} is disabled (enable it in Settings)")
    # A mask ("••••1234") or any non-ASCII character can only reach this far if
    # a settings write path persisted a display value instead of the secret.
    # Every protocol sends the key in a header, and httpx rejects non-ASCII
    # header values with a bare `UnicodeEncodeError: 'ascii' codec ...` raised
    # deep in its internals — fail here with something the user can act on.
    _key = str(cfg.get("api_key") or "")
    if prov_mod.is_masked(_key) or not _key.isascii():
        raise ValueError(
            f"API key for {provider_id} is a mask placeholder or contains "
            "non-ASCII characters — re-enter the real key in Settings → Model API"
        )

    proto = cfg.get("protocol")
    # unified name resolution: explicit override → default_model/model field →
    # first entry of models[] (the v2 list form)
    model = (
        model_name
        or prov_mod.model_for_provider(all_prov, provider_id)
        or prov_mod.model_for_config(cfg)
    )
    temperature, max_tokens = _sampling(cfg)
    base_url = cfg.get("base_url") or None
    # 思考档位(设计 §3):一次解析三个分支共用;auto(None)时各分支维持
    # 现状(provider 配置的 enable_thinking 等继续生效)。
    effort = _effective_effort(cfg, thinking_effort)

    if proto == "anthropic":
        key = cfg.get("api_key")
        if not key:
            raise ValueError("Anthropic API key is empty — fill it in Settings → Model API")
        # Identify Ginno to gateways: langchain-anthropic stamps every request
        # with its own ``User-Agent: langchain-anthropic/<ver>`` (its
        # chat_models._USER_AGENT), so external gateways (magpie, cc-switch
        # usage views) attribute Ginno's traffic to "langchain-anthropic".
        # User-supplied default_headers are merged OVER these by
        # langchain-anthropic, so ours win here. bearer_auth's Authorization
        # rides on the same dict instead of replacing the UA.
        default_headers: dict[str, str] = {"User-Agent": ginno_user_agent()}
        # Session affinity for external gateways (magpie etc.): magpie reads
        # x-session-id ahead of its content-fingerprint fallback, giving
        # exact per-session routing (incl. subagent isolation — each subagent
        # is its own ginno session_id) instead of first-user-message hashing.
        if session_id:
            default_headers["x-session-id"] = session_id
        if cfg.get("bearer_auth"):
            default_headers["Authorization"] = f"Bearer {key}"
        # Anthropic 协议必须显式 max_tokens:_sampling 的 None(0=不传,端点
        # 自决)在这里没有"不传"可走,退回老默认 4096。
        mt = max_tokens if (max_tokens is not None and max_tokens > 0) else 4096
        chat_kwargs: dict[str, Any] = dict(
            model=model or "claude-3-7-sonnet-20250219",
            api_key=key,
            base_url=base_url,
            temperature=temperature if temperature is not None else 0.7,
            max_tokens=mt,
            streaming=True,
            timeout=_chat_timeout(cfg),
            max_retries=6,
            default_headers=default_headers,
        )
        if effort:
            if effort == "off":
                # 显式关(网关可能默认开);多数 Claude 模型等价于不传。
                chat_kwargs["thinking"] = {"type": "disabled"}
            else:
                budget = thinking_budgets(cfg)[effort]
                chat_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                # Anthropic 约束:开思考时 temperature 必须 1,且 max_tokens
                # 必须大于 budget —— 抬到 max(现值, budget + 4096) 保证成立
                #(mt 为 4096 fallback 时同样被抬升;budget 预留余量)。
                chat_kwargs["temperature"] = 1.0
                chat_kwargs["max_tokens"] = max(
                    int(chat_kwargs.get("max_tokens") or 0), budget + 4096
                )
        m = _healing_chat_anthropic()(**chat_kwargs)
        if server_web_search_on(cfg):
            m = _anthropic_with_server_search(m, cfg)
        return m

    if proto == "openai-responses":
        # Official OpenAI Responses API (and Responses-compatible gateways).
        # org_id → openai_organization (official OpenAI only); the compat
        # extra_body switches (enable_search/enable_thinking) have no
        # Responses equivalent here.
        chat_kw: dict[str, Any] = dict(
            model=model or "gpt-4o",
            api_key=cfg.get("api_key") or "not-needed",
            base_url=base_url or "https://api.openai.com/v1",
            temperature=temperature if temperature is not None else 0.7,
            streaming=True,
            timeout=_chat_timeout(cfg),
            max_retries=6,
            use_responses_api=True,
        )
        if max_tokens:  # None(0=不传)不传——openai 协议不强制,端点自决
            chat_kw["max_tokens"] = max_tokens
        if cfg.get("org_id"):
            chat_kw["openai_organization"] = cfg["org_id"]
        # Same gateway-identity override as the anthropic branch above: the
        # openai SDK stamps AsyncOpenAI/Python, which magpie attributes to
        # "AsyncOpenAI". SDK default_headers merge over its own UA, so ours wins.
        default_headers = {"User-Agent": ginno_user_agent()}
        if session_id:
            default_headers["x-session-id"] = session_id
        chat_kw["default_headers"] = default_headers
        if effort:
            # Responses API:langchain-openai 把 reasoning_effort 转成
            # reasoning={"effort": ...};off 映射 minimal(o 系/gpt-5 系最低
            # 档)。不支持思考的模型由端点报错透传错误卡(设计 §7)。
            chat_kw["reasoning_effort"] = "minimal" if effort == "off" else effort
        return _responses_chat_openai()(**chat_kw)

    # openai / openai-compatible
    key = cfg.get("api_key") or ""
    if proto == "openai" and not base_url:
        base_url = "https://api.openai.com/v1"
    chat_kw: dict[str, Any] = dict(
        model=model or "gpt-4o",
        api_key=key or "not-needed",
        base_url=base_url,
        temperature=temperature if temperature is not None else 0.7,
        streaming=True,
        timeout=_chat_timeout(cfg),
        max_retries=6,
    )
    if max_tokens:  # None(0=不传)不传——openai 协议不强制,端点自决
        chat_kw["max_tokens"] = max_tokens
    # OpenAI-compatible gateway switches, forwarded verbatim into the request
    # body via `extra_body` (langchain-openai passes it through untouched):
    # - `enable_search` (None = follow the provider config) lets gateways such
    #   as Qwen / DashScope compatible-mode run the model's built-in web search.
    # - `enable_thinking` turns on thinking mode for hybrid-thinking models
    #   (Qwen3 commercial line defaults to OFF server-side); the endpoint then
    #   streams `reasoning_content` deltas, which ReasoningChatOpenAI below
    #   re-attaches to additional_kwargs for the thinking.delta pipeline.
    extra_body: dict[str, Any] = {}
    es = cfg.get("enable_search") if enable_search is None else enable_search
    if es:
        extra_body["enable_search"] = True
    if effort:
        # 会话档位覆盖 provider 配置(设计 §3):有档位时 cfg.enable_thinking
        # 不再参与;方言决定参数形状。
        dialect = thinking_dialect(cfg, model)
        if dialect == "qwen":
            # DashScope 兼容模式:开关 + 预算(商业版 qwen3 系)。
            if effort == "off":
                extra_body["enable_thinking"] = False
            else:
                extra_body["enable_thinking"] = True
                extra_body["thinking_budget"] = thinking_budgets(cfg)[effort]
        elif dialect == "glm":
            # GLM(bigmodel)只有开关,无预算概念:off=关,其余全落开。
            extra_body["thinking"] = {
                "type": "disabled" if effort == "off" else "enabled"
            }
        else:
            # deepseek / generic:尽力透传 reasoning_effort(chat completions
            # 参数);不支持的老网关报错原文回错误卡,用户可回落 auto。
            chat_kw["reasoning_effort"] = "minimal" if effort == "off" else effort
    elif cfg.get("enable_thinking"):
        extra_body["enable_thinking"] = True
    if extra_body:
        chat_kw["extra_body"] = extra_body
    # Same gateway-identity override as the anthropic branch above: the
    # openai SDK stamps AsyncOpenAI/Python, which magpie attributes to
    # "AsyncOpenAI". SDK default_headers merge over its own UA, so ours wins.
    default_headers = {"User-Agent": ginno_user_agent()}
    if session_id:
        default_headers["x-session-id"] = session_id
    chat_kw["default_headers"] = default_headers
    # Reasoning round-trip (see _replay_reasoning_content): on for
    # openai-compatible gateways (DeepSeek thinking mode requires it, Qwen
    # ignores it), off for the official OpenAI endpoint. An explicit
    # ``reasoning_passthrough`` in the config wins — the escape hatch for a
    # gateway that rejects the non-standard field.
    passthrough = cfg.get("reasoning_passthrough")
    chat_kw["reasoning_passthrough"] = (
        proto == "openai-compatible" if passthrough is None else bool(passthrough)
    )
    return _reasoning_chat_openai()(**chat_kw)


def build_model_by_name(model_name: str):
    """Build a chat model from a bare model name (master-plan §2.2 checklist M).

    ``extract_model`` in a DSL node is a single string (e.g. a cheap model id),
    but ``build_model`` keys on config id. Resolve the name to a config:
    (1) an enabled config that lists the name in its ``models[]`` (or as its
        default model — covers legacy records that only carry ``model``/
        ``default_model`` without a list),
    (2) an enabled config whose *id* equals the name,
    (3) the default config with the name passed as a model override.
    """
    all_prov = prov_mod.load_providers()
    for pid, cfg in all_prov.items():
        if not cfg.get("enabled"):
            continue
        known = set(cfg.get("models") or [])
        for key in ("default_model", "model"):
            if cfg.get(key):
                known.add(cfg[key])
        if model_name in known:
            return build_model(pid, model_name)
    if model_name in all_prov and all_prov[model_name].get("enabled"):
        return build_model(model_name)
    default = prov_mod.get_default_provider()
    return build_model(default, model_name)
