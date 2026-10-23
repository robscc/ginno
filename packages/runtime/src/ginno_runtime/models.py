"""Model factory — build a LangChain chat model from a provider id.

Reads provider config from settings.json `providers` (see providers.py).
Sampling params: temperature as a top-level kwarg; max_tokens via
model_kwargs (robust across langchain versions). base_url + api_key as
top-level kwargs (verified present on ChatAnthropic + ChatOpenAI).
"""

from __future__ import annotations

import os
from typing import Any

from . import providers as prov_mod

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


def _sampling(cfg: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    """Sampling params for the chat constructors.

    max_tokens 语义(2026-10-16,office-bench 根因修复):正值 → 传;0 → 完全
    不传(交由端点自决——thinking 模型的 reasoning 计入输出预算,一个过小的
    上限会让重任务在思考阶段耗尽预算、content 一个字不吐);缺省(None,
    只有未 normalize 的调用方)→ 沿用历史默认 8192。
    """
    temperature = cfg.get("temperature")
    model_kwargs: dict[str, Any] = {}
    mt = cfg.get("max_tokens")
    if mt is None:
        model_kwargs["max_tokens"] = 8192
    elif int(mt) > 0:
        model_kwargs["max_tokens"] = int(mt)
    return (float(temperature) if temperature is not None else None, model_kwargs)


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


def build_model(provider_id: str, model_name: str | None = None, enable_search: bool | None = None):
    """Return a LangChain chat model for the given provider id.

    `model_name` overrides the provider's configured default/model.
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

    proto = cfg.get("protocol")
    # unified name resolution: explicit override → default_model/model field →
    # first entry of models[] (the v2 list form)
    model = (
        model_name
        or prov_mod.model_for_provider(all_prov, provider_id)
        or prov_mod.model_for_config(cfg)
    )
    temperature, model_kwargs = _sampling(cfg)
    base_url = cfg.get("base_url") or None

    if proto == "anthropic":
        key = cfg.get("api_key")
        if not key:
            raise ValueError("Anthropic API key is empty — fill it in Settings → Model API")
        chat_kwargs: dict[str, Any] = dict(
            model=model or "claude-3-7-sonnet-20250219",
            api_key=key,
            base_url=base_url,
            temperature=temperature if temperature is not None else 0.7,
            model_kwargs=model_kwargs or {"max_tokens": 4096},
            streaming=True,
            timeout=_chat_timeout(cfg),
            max_retries=6,
        )
        # Some Anthropic-compatible gateways (corporate model hubs / proxies) expect
        # the token in `Authorization: Bearer ...` instead of `x-api-key`. The official
        # Anthropic API uses x-api-key, so this is opt-in via `bearer_auth`.
        if cfg.get("bearer_auth"):
            chat_kwargs["default_headers"] = {"Authorization": f"Bearer {key}"}
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
            model_kwargs=model_kwargs,
            streaming=True,
            timeout=_chat_timeout(cfg),
            max_retries=6,
            use_responses_api=True,
        )
        if cfg.get("org_id"):
            chat_kw["openai_organization"] = cfg["org_id"]
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
        model_kwargs=model_kwargs,
        streaming=True,
        timeout=_chat_timeout(cfg),
        max_retries=6,
    )
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
    if cfg.get("enable_thinking"):
        extra_body["enable_thinking"] = True
    if extra_body:
        chat_kw["extra_body"] = extra_body
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
