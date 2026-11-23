"""Keep provider-specific output in its original request scope."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

_SOURCE = "ginno_model_origin"
_USER_SERVER_ITEMS = {
    "apply_patch_call_output",
    "configuration_update",
    "item_reference",
    "mcp_approval_response",
    "tool_search_output",
}
_CALL_FIELDS = {
    "function_call": ("call_id", "arguments"),
    "tool_call": ("id", "args"),
    "tool_use": ("id", "input"),
}
_SERVER_CALLS = {"custom_tool_call", "computer_call", "server_tool_call", "server_tool_use"}


def model_origin(provider_id: str, model: str, endpoint: str, protocol: str, tag: str) -> str:
    # A URL can itself carry credentials. Only persist the fingerprint.
    identity = json.dumps([provider_id, model, endpoint.rstrip("/"), protocol, tag])
    return sha256(identity.encode()).hexdigest()


def stamp_model_origin(bound: Any, message: Any) -> None:
    """Stamp the message from the actual bound model used by the graph call."""
    model = getattr(bound, "bound", bound)
    origin_for_request = getattr(model, "_history_origin", None)
    if isinstance(message, AIMessage) and callable(origin_for_request):
        origin = origin_for_request(getattr(bound, "kwargs", {}))
        if isinstance(origin, str) and origin:
            message.additional_kwargs[_SOURCE] = origin


def _unwrap(block: dict) -> dict:
    if block.get("type") == "non_standard" and isinstance(block.get("value"), dict):
        return block["value"]
    return block


def _portable_assistant(message: AIMessage) -> AIMessage | None:
    # Rebuild from visible text and normalized function calls. In particular,
    # older Responses adapters reconstruct wire IDs from message.id and kwargs.
    if isinstance(message.content, str):
        blocks = [message.content]
    else:
        blocks = [_unwrap(block) if isinstance(block, dict) else block for block in message.content]
    text = []
    calls = {}
    server_calls = set()
    for block in blocks:
        if isinstance(block, str):
            text.append(block)
            continue
        kind = block.get("type")
        if kind in ("text", "output_text", "refusal"):
            value = block.get("refusal" if kind == "refusal" else "text")
            if isinstance(value, str):
                text.append(value)
        elif kind in _SERVER_CALLS or (
            kind == "tool_use" and isinstance(caller := block.get("caller"), dict)
            and caller.get("type") != "direct"
        ):
            # Server-orchestrated calls may also appear in AIMessage.tool_calls.
            # Anthropic's direct caller marks a normal client tool.
            call_id = block.get("call_id") or block.get("id")
            if isinstance(call_id, str):
                server_calls.add(call_id)
        elif kind in _CALL_FIELDS:
            id_key, args_key = _CALL_FIELDS[kind]
            call_id, name, args = block.get(id_key), block.get("name"), block.get(args_key)
            if kind == "function_call" and isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if (
                isinstance(call_id, str)
                and call_id
                and isinstance(name, str)
                and name
                and isinstance(args, dict)
            ):
                calls[call_id] = {"name": name, "args": args, "id": call_id}
    if not any(text) and isinstance(refusal := message.additional_kwargs.get("refusal"), str):
        text.append(refusal)
    for call in message.tool_calls:
        if call.get("id") and call.get("name") and isinstance(call.get("args"), dict):
            calls[call["id"]] = {key: call[key] for key in ("name", "args", "id")}
    portable_calls = [call for call_id, call in calls.items() if call_id not in server_calls]
    visible = "".join(text)
    if not visible.strip() and not portable_calls:
        return None
    # Empty assistant text is rejected by some gateways even alongside a call.
    return AIMessage(content=visible if visible.strip() else [], tool_calls=portable_calls)


def _portable_result_blocks(blocks: list) -> list[dict]:
    result = []
    for block in blocks:
        if isinstance(block, str):
            if block:
                result.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        block = _unwrap(block)
        kind = block.get("type")
        if kind in ("text", "output_text", "input_text") and isinstance(block.get("text"), str):
            if block["text"]:
                result.append({"type": "text", "text": block["text"]})
        elif kind == "image_url" and isinstance(block.get("image_url"), dict):
            if url := block["image_url"].get("url"):
                result.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "tool_result":
            content = block.get("content")
            if isinstance(content, (str, list)):
                result.extend(
                    _portable_result_blocks([content] if isinstance(content, str) else content)
                )
    return result


def _portable_tool_result(message: ToolMessage) -> ToolMessage:
    content = message.content
    if isinstance(content, list):
        content = _portable_result_blocks(content) or "(no portable tool output)"
    return ToolMessage(
        content=content,
        name=message.name,
        tool_call_id=message.tool_call_id,
        status=message.status,
    )


def request_history(messages: list[Any], origin: str) -> list[Any]:
    """Make a request copy. Unattributed history can carry text and functions.

    Tool results inherit their call's origin within the same turn. Repair of
    ordering and interrupted calls runs after this pass.
    """
    if not origin:
        return messages
    output = []
    same_source_calls = set()
    turn_start = 0
    same_source_assistant = False

    def finish_turn() -> None:
        for idx in range(turn_start, len(output)):
            message = output[idx]
            if isinstance(message, ToolMessage) and message.tool_call_id not in same_source_calls:
                output[idx] = _portable_tool_result(message)
        same_source_calls.clear()

    for message in messages:
        if isinstance(message, (HumanMessage, SystemMessage)):
            finish_turn()
            turn_start = len(output)
            if not same_source_assistant and isinstance(message.content, list):
                content = [
                    block
                    for block in message.content
                    if not isinstance(block, dict)
                    or _unwrap(block).get("type") not in _USER_SERVER_ITEMS
                ]
                if not content and message.content:
                    continue
                if len(content) != len(message.content):
                    message = message.model_copy(update={"content": content})
            same_source_assistant = False
        if isinstance(message, AIMessage):
            same_source_assistant = message.additional_kwargs.get(_SOURCE) == origin
            if same_source_assistant:
                same_source_calls.update(call.get("id") for call in message.tool_calls)
            else:
                message = _portable_assistant(message)
                if message is None:
                    continue
        output.append(message)
    finish_turn()
    return output
