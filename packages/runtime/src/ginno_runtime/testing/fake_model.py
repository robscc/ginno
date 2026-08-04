"""A deterministic, scriptable fake chat model for tests and the ``GINNO_FAKE_LLM`` seam.

``ScriptedChatModel`` replays a fixed list of :class:`AIMessage` turns (plain text
or tool calls) instead of calling a real LLM. Driving the *real* compiled LangGraph
with it exercises the whole agent loop — tool execution, permission interrupts,
checkpointer persistence — with zero network and no API key.

Design notes (empirically validated against langchain-core 1.4.x + langgraph 1.2.x):

* Override the **internal** ``_generate`` (drives the node return value, the
  ``updates`` stream mode, and graph routing) and ``_astream`` (drives the
  ``messages`` stream mode the WebSocket layer turns into ``token.delta`` /
  ``tool.start`` events). ``bind_tools`` must be overridden too — the base class
  raises ``NotImplementedError`` and ``graph.agent_node`` calls it whenever the
  active agent allows any tool.
* To make the server emit ``tool.start``, the streamed tool call is split into a
  **name-first chunk with empty args** followed by an args chunk (see
  ``server._stream_graph``: it only fires ``tool.start`` when the chunk has a
  name, no index, and empty args).
* Structured-output tools (``render_widget`` / ``attach_ref``) and ``workflow_*``
  are surfaced by the server from the **complete** ``tool_calls`` on the AIMessage
  in ``updates`` mode — so put the full ``tool_calls`` on the ``_generate`` return
  value; the streaming chunk shape is irrelevant for them.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr


def script_tool_call(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> dict:
    """Build a LangChain-style ``tool_call`` dict for use in :func:`script`."""
    return {
        "name": name,
        "args": args or {},
        "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
        "type": "tool_call",
    }


def script(
    text: str = "",
    tool_calls: list[dict] | None = None,
    msg_id: str | None = None,
    usage: dict | None = None,
) -> AIMessage:
    """Build one scripted assistant turn — plain text and/or tool calls.

    ``usage`` attaches ``usage_metadata`` (provider token counters) so tests
    can exercise usage telemetry (plan D1/D2) end to end.
    """
    return AIMessage(
        content=text,
        tool_calls=tool_calls or [],
        id=msg_id or f"fake_{uuid.uuid4().hex[:8]}",
        **({"usage_metadata": usage} if usage else {}),
    )


class ScriptedChatModel(BaseChatModel):
    """Replay a fixed list of AIMessage turns. Each call pops the next turn."""

    scripts: list[AIMessage] = []
    _i: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "ginno-scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        # The graph calls this whenever the agent allows a tool; the fake ignores
        # the toolset and just replays its script. Returning self keeps the script
        # pointer intact across the bind.
        return self

    def _next(self, messages: Any = None) -> AIMessage:
        # No scripts at all (e.g. GINNO_FAKE_LLM set without GINNO_FAKE_LLM_SCRIPTS):
        # run in "placeholder" mode and answer every turn with a self-explanatory
        # message that echoes the question and explains how to get a real answer,
        # so the demo never looks like a silent "OK" bug.
        if not self.scripts:
            return self._placeholder(messages)
        # Script exhausted mid-conversation: return a terminal tool-less message so
        # the graph always reaches END. Without this, a block/ask that routes back
        # to the agent (Command(goto="agent")) would re-offer the last tool and
        # loop forever (never hitting the recursion limit, so no error event would
        # fire and a WS test would hang).
        if self._i >= len(self.scripts):
            return AIMessage(content="")
        msg = self.scripts[self._i]
        self._i += 1
        return msg

    @staticmethod
    def _placeholder(messages: Any) -> AIMessage:
        question = ""
        for m in reversed(messages or []):
            if getattr(m, "type", "") == "human":
                c = getattr(m, "content", "")
                question = c if isinstance(c, str) else str(c)
                break
        q = question.strip() or "（空）"
        # NOTE: the chat bubble renders markdown now, but keep this free of
        # ** / > / ` markers anyway so it reads cleanly as plain instructions.
        text = (
            "[ GINNO 测试 / 演示模型 ]\n\n"
            "当前运行时启用了环境变量 GINNO_FAKE_LLM，且没有通过 GINNO_FAKE_LLM_SCRIPTS "
            "提供预设回复，所以这条回复由“占位模型”生成，并没有调用任何真实大模型。\n\n"
            f"你的问题：{q}\n\n"
            "想要真实回答，请：\n"
            "1) 在「设置 → 模型 API」里启用并“验证”通过一个模型提供商；\n"
            "2) 重启运行时，并去掉 GINNO_FAKE_LLM 这个环境变量。\n\n"
            "（如果只想让这个演示模型回复别的内容，可把 GINNO_FAKE_LLM_SCRIPTS 指向一个 JSON 回复列表。）"
        )
        return AIMessage(content=text)

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        yield from self._iter_chunks(self._next(messages))

    async def _astream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        for chunk in self._iter_chunks(self._next(messages)):
            yield chunk

    @staticmethod
    def _iter_chunks(msg: AIMessage):
        """Yield ChatGenerationChunks for the messages stream mode.

        The LAST chunk carries the scripted message's ``usage_metadata`` (when
        set), mirroring real providers that report usage on the final chunk —
        this drives the server's usage telemetry (plan D1/D2) in tests.
        """
        cid = msg.id or f"fake_{uuid.uuid4().hex[:8]}"
        usage = getattr(msg, "usage_metadata", None)
        if msg.tool_calls:
            last = len(msg.tool_calls) - 1
            for i, tc in enumerate(msg.tool_calls):
                # 1) name-first chunk with empty args → fires server "tool.start"
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        id=cid,
                        tool_call_chunks=[
                            {"name": tc["name"], "id": tc.get("id"), "args": "", "index": 0}
                        ],
                    )
                )
                # 2) args chunk (final one carries usage)
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        id=cid,
                        tool_call_chunks=[
                            {"name": None, "id": None, "args": json.dumps(tc.get("args") or {}), "index": 0}
                        ],
                        **({"usage_metadata": usage} if i == last and usage else {}),
                    )
                )
        else:
            content = msg.content if isinstance(msg.content, str) else ""
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=content,
                    id=cid,
                    **({"usage_metadata": usage} if usage else {}),
                )
            )


def _scripts_from_env() -> list[AIMessage]:
    """Parse ``GINNO_FAKE_LLM_SCRIPTS`` into a turn list.

    Accepts either a path to a JSON file or an inline JSON string. The JSON is a
    list of turns: ``[{"content": "...", "tool_calls": [{"name", "args"}]}, ...]``.
    Returns an **empty** list when unset/invalid — in that case ScriptedChatModel
    runs in placeholder mode (see ``_placeholder``) instead of replying a bare
    canned string.
    """
    raw = os.environ.get("GINNO_FAKE_LLM_SCRIPTS", "")
    if not raw:
        return []
    try:
        if os.path.exists(raw):
            with open(raw, encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return []

    turns: list[AIMessage] = []
    for item in data if isinstance(data, list) else [data]:
        calls = [
            script_tool_call(tc.get("name", ""), tc.get("args") or {})
            for tc in (item.get("tool_calls") or [])
        ]
        turns.append(
            script(
                text=item.get("content", ""),
                tool_calls=calls,
                usage=item.get("usage"),  # optional per-turn token counters
            )
        )
    return turns


def build_fake_model() -> ScriptedChatModel:
    """Construct a :class:`ScriptedChatModel` from ``GINNO_FAKE_LLM_SCRIPTS``.

    Used by the ``GINNO_FAKE_LLM`` seam in :func:`ginno_runtime.models.build_model`
    so a subprocess-launched server (e.g. a full-process e2e) is deterministic
    without a real provider.
    """
    return ScriptedChatModel(scripts=_scripts_from_env())
