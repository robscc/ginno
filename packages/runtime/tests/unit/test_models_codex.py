"""Exercise saved tags and portable history through the real SDK serializers."""

import json

import anthropic
import httpx
import httpx2
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime import providers
from ginno_runtime.model_history import stamp_model_origin
from ginno_runtime.models import build_model

pytestmark = pytest.mark.unit
MODEL = "gpt-6.2-astra"
ECHO = {"name": "echo", "description": "Echo text", "parameters": {"type": "object"}}
CALL = {"name": "echo", "args": {"text": "tool input"}, "id": "call_echo"}


def _cfg(protocol, *, tagged=True, model=MODEL, cap="ultra", **overrides):
    tags = {model: {"tag": "codex", "max_effort": cap}} if tagged else {}
    return {
        "enabled": True,
        "protocol": protocol,
        "model": model,
        "api_key": "test-key",
        "base_url": "https://mock.invalid/v1",
        "model_tags": tags,
        "enable_thinking": True,
        "enable_search": True,
        "temperature": 0.4,
        "max_tokens": 512,
        **overrides,
    }


def _capture_sdk(model, protocol, *, messages=None, tools=False, assert_request=None):
    requests = []
    http = httpx2 if protocol == "anthropic" else httpx

    def handle(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if assert_request:
            assert_request(body)
        if protocol == "anthropic":
            return http.Response(
                200,
                json={
                    "id": "msg_answer",
                    "type": "message",
                    "role": "assistant",
                    "model": model.model,
                    "content": [{"type": "text", "text": "answer"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        if protocol == "openai-responses":
            return http.Response(
                200,
                json={
                    "id": "resp_answer",
                    "object": "response",
                    "model": model.model_name,
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "id": "msg_answer",
                            "content": [
                                {"type": "output_text", "text": "answer", "annotations": []}
                            ],
                        }
                    ],
                },
            )
        return http.Response(
            200,
            json={
                "id": "chatcmpl_answer",
                "object": "chat.completion",
                "model": model.model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with http.Client(transport=http.MockTransport(handle)) as client:
        model.streaming = False
        if protocol == "anthropic":
            model.__dict__["_client"] = anthropic.Anthropic(
                api_key="test-key",
                base_url="https://mock.invalid",
                http_client=client,
                timeout=10,
            )
        else:
            sdk = openai.OpenAI(
                api_key="test-key", base_url="https://mock.invalid/v1", http_client=client
            )
            model.root_client = sdk
            model.client = sdk.chat.completions
        bound = model.bind_tools([ECHO]) if tools else model
        bound.invoke(messages or [HumanMessage("go")])
    (request,) = requests
    return request


@pytest.mark.parametrize(
    ("protocol", "path", "field"),
    [
        ("openai-responses", "/v1/responses", None),
        ("openai-compatible", "/v1/chat/completions", None),
        ("anthropic", "/v1/messages", "output_config"),
        ("anthropic", "/v1/messages", "reasoning_effort"),
    ],
)
def test_codex_ultra_real_sdk(monkeypatch, protocol, path, field):
    cfg = _cfg(protocol)
    if field:
        cfg["model_tags"][MODEL]["anthropic_effort_field"] = field
        cfg["extra_body"] = {
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "output_config": {"effort": "low"},
            "reasoning_effort": "medium",
            "extra_body": {"enable_thinking": True, "metadata": {"user_id": "test-user"}},
        }
    monkeypatch.setattr(providers, "load_providers", lambda: {"p": cfg})
    actual_path, body = _capture_sdk(
        build_model("p", thinking_effort="ultra"), protocol, tools=True
    )
    assert actual_path == path
    if protocol == "anthropic":
        if field == "output_config":
            assert body["output_config"] == {"effort": "ultra"}
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == "ultra"
            assert "output_config" not in body
        assert body["metadata"] == {"user_id": "test-user"}
        assert body["temperature"] == 0.4
        assert not (body.keys() & {"thinking", "enable_thinking", "budget_tokens", "effort"})
    else:
        assert not (body.keys() & {"temperature", "enable_thinking", "enable_search"})
        if protocol == "openai-responses":
            assert body["reasoning"] == {"effort": "ultra"}
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == "ultra"


def test_identical_model_name_uses_saved_tag_only(monkeypatch):
    model_name = "codex/gpt-5.3-codex"
    configs = {
        "tagged": _cfg("anthropic", model=model_name),
        "default": _cfg("anthropic", model=model_name, tagged=False),
    }
    monkeypatch.setattr(providers, "load_providers", lambda: configs)
    _, tagged = _capture_sdk(build_model("tagged", thinking_effort="high"), "anthropic")
    _, default = _capture_sdk(build_model("default", thinking_effort="high"), "anthropic")
    assert tagged["output_config"] == {"effort": "high"}
    assert default["thinking"] == {"type": "enabled", "budget_tokens": 32768}
    assert default["temperature"] == 1.0
    assert "output_config" not in default


def test_codex_tag_precedes_provider_wide_thinking_settings(monkeypatch):
    cfg = _cfg("anthropic", thinking_mode="none", thinking_param_style="reasoning_effort")
    monkeypatch.setattr(providers, "load_providers", lambda: {"p": cfg})
    _, body = _capture_sdk(build_model("p", thinking_effort="ultra"), "anthropic")
    assert body["output_config"] == {"effort": "ultra"}
    assert "reasoning_effort" not in body
    assert "thinking" not in body


@pytest.mark.parametrize(
    "cap, configured, chosen, expected",
    [
        ("xhigh", "medium", "auto", "medium"),
        ("xhigh", "ultra", "max", None),
        (None, "low", "high", None),
        ("ultra", "high", "off", "low"),
    ],
)
def test_codex_effort_never_exceeds_saved_cap(monkeypatch, cap, configured, chosen, expected):
    cfg = _cfg("anthropic", cap=cap, extra_body={"output_config": {"effort": configured}})
    monkeypatch.setattr(providers, "load_providers", lambda: {"p": cfg})
    _, body = _capture_sdk(build_model("p", thinking_effort=chosen), "anthropic")
    assert body.get("output_config", {}).get("effort") == expected


@pytest.mark.parametrize(
    ("protocol", "model_name", "overrides", "stamped"),
    [
        ("openai-responses", "another", {}, True),
        ("openai-responses", MODEL, {"base_url": "https://other.invalid/v1"}, True),
        ("openai-compatible", MODEL, {}, True),
        ("anthropic", MODEL, {}, True),
        ("openai-responses", MODEL, {"tagged": False}, True),
        ("openai-responses", MODEL, {}, False),
        ("openai-compatible", "deepseek-v4.1-flash", {"tagged": False}, False),
    ],
)
def test_history_only_replays_portable_fields_after_switch(
    monkeypatch, protocol, model_name, overrides, stamped
):
    configs = {"p": _cfg("openai-responses")}
    monkeypatch.setattr(providers, "load_providers", lambda: configs)
    source = build_model("p")
    assistant = AIMessage(
        id="msg_private",
        response_metadata={"id": "resp_private"},
        content=[
            {"type": "text", "text": "visible answer", "id": "msg_private"},
            {"type": "reasoning", "id": "rs_private", "encrypted_content": "secret-ciphertext"},
            {"type": "thinking", "thinking": "hidden-thought", "signature": "secret-signature"},
            {"type": "file_search_call", "id": "fs_private"},
            {
                "type": "function_call",
                "id": "fc_private",
                "call_id": "call_echo",
                "name": "echo",
                "arguments": '{"text":"tool input"}',
            },
            {"type": "custom_tool_call", "id": "ct_private", "call_id": "call_custom"},
        ],
        tool_calls=[
            CALL,
            {"name": "custom", "args": {"__arg1": "private input"}, "id": "call_custom"},
        ],
        additional_kwargs={
            "reasoning": {"id": "rs_legacy", "encrypted_content": "secret-legacy"},
            "reasoning_content": "secret-summary",
            "tool_outputs": [{"type": "mcp_call", "id": "mcp_private"}],
        },
    )
    if stamped:
        stamp_model_origin(source, assistant)
    messages = [
        HumanMessage("go"),
        assistant,
        ToolMessage(content="tool result", tool_call_id="call_echo"),
        ToolMessage(content="private result", tool_call_id="call_custom"),
        HumanMessage("continue"),
    ]
    original = [message.model_dump_json() for message in messages]
    configs["p"] = _cfg(protocol, model=model_name, **overrides)

    def validate_wire(body):
        wire = json.dumps(body)
        assert not any(
            token in wire
            for token in ("_private", "secret-", "hidden-thought", "call_custom", "private result")
        )
        assert "visible answer" in wire and "tool input" in wire and "tool result" in wire
        assert wire.count("call_echo") == 2
        if protocol == "openai-compatible":
            expected = None if configs["p"]["model_tags"] else ""
            assert body["messages"][1].get("reasoning_content") == expected

    _capture_sdk(build_model("p"), protocol, messages=messages, assert_request=validate_wire)
    assert [message.model_dump_json() for message in messages] == original


def test_cross_protocol_replays_anthropic_direct_tool_and_result(monkeypatch):
    from anthropic.types import Message

    configs = {
        "source": _cfg("anthropic"),
        "target": _cfg("openai-compatible", model="plain-model", tagged=False),
    }
    monkeypatch.setattr(providers, "load_providers", lambda: configs)
    source = build_model("source")
    response = Message.model_validate({
        "id": "msg_tool", "role": "assistant", "type": "message", "model": MODEL,
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{
            "type": "tool_use", "id": "toolu_direct", "caller": {"type": "direct"},
            "name": "lookup", "input": {"key": "example"},
        }],
    })
    assistant = source._format_output(response).generations[0].message
    stamp_model_origin(source, assistant)
    history = [
        HumanMessage("Lookup"), assistant,
        ToolMessage(content="lookup result", tool_call_id="toolu_direct"),
        HumanMessage("Continue"),
    ]
    original = [message.model_dump_json() for message in history]
    _, body = _capture_sdk(build_model("target"), "openai-compatible", messages=history)
    wire = body["messages"]
    assert wire[1]["tool_calls"] == [{
        "type": "function", "id": "toolu_direct",
        "function": {"name": "lookup", "arguments": '{"key": "example"}'},
    }]
    assert wire[2] == {"role": "tool", "tool_call_id": "toolu_direct", "content": "lookup result"}
    assert [message.model_dump_json() for message in history] == original


@pytest.mark.parametrize("protocol", ["openai-responses", "anthropic", "openai-compatible"])
def test_same_origin_keeps_protocol_history(monkeypatch, protocol):
    content = {
        "openai-responses": [
            {
                "type": "reasoning",
                "id": "rs_original",
                "summary": [],
                "encrypted_content": "original",
            },
            {"type": "text", "id": "msg_original", "text": "answer"},
        ],
        "anthropic": [
            {"type": "thinking", "thinking": "thought", "signature": "original"},
            {"type": "text", "text": "answer"},
        ],
        "openai-compatible": "answer",
    }[protocol]
    cfg = _cfg(
        protocol,
        tagged=False,
        model="deepseek-v4.1-flash" if protocol == "openai-compatible" else MODEL,
    )
    monkeypatch.setattr(providers, "load_providers", lambda: {"p": cfg})
    assistant = AIMessage(
        content=content, tool_calls=[CALL], additional_kwargs={"reasoning_content": "original"}
    )
    stamp_model_origin(build_model("p"), assistant)
    history = [
        HumanMessage("go"),
        assistant,
        ToolMessage(content="result", tool_call_id="call_echo"),
    ]
    _, body = _capture_sdk(build_model("p"), protocol, messages=history)
    wire = json.dumps(body)
    assert "original" in wire and wire.count("call_echo") == 2


def test_legacy_responses_continuation_reference_is_filtered(monkeypatch):
    cfg = _cfg("openai-responses")
    monkeypatch.setattr(providers, "load_providers", lambda: {"p": cfg})
    model = build_model("p")
    model.use_previous_response_id = True
    assistant = AIMessage(
        content=[{"type": "text", "text": "old answer"}],
        id="msg_legacy",
        response_metadata={"id": "resp_legacy"},
        additional_kwargs={"reasoning": {"type": "reasoning", "id": "rs_legacy", "summary": []}},
    )
    messages = [HumanMessage("go"), assistant, HumanMessage("next")]
    payload = model._get_request_payload(messages, previous_response_id="resp_legacy")
    assert "previous_response_id" not in payload
    assert "old answer" in json.dumps(payload)
    assert "_legacy" not in json.dumps(payload)

    stamp_model_origin(model, assistant)
    payload = model._get_request_payload(messages)
    assert payload["previous_response_id"] == "resp_legacy"
    assert len(payload["input"]) == 1


async def test_graph_stream_uses_saved_route_and_stamps_actual_response(monkeypatch):
    from ginno_runtime.graph import _ainvoke_rate_limited

    configs = {"p": _cfg("openai-compatible", reasoning_passthrough=True)}
    monkeypatch.setattr(providers, "load_providers", lambda: configs)
    requests = []

    def handle(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["stream"] and body["reasoning_effort"] == "ultra"
        requests.append(body)
        chunk = {
            "id": "chatcmpl_mock",
            "model": MODEL,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "delta": {
                        "role": "assistant",
                        "content": "visible answer",
                        "reasoning_content": "private thought",
                    },
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    first_message = HumanMessage("go")
    model = build_model("p", thinking_effort="ultra")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        sdk = openai.AsyncOpenAI(
            api_key="test-key",
            base_url="https://mock.invalid/v1",
            http_client=transport,
        )
        model.root_async_client = sdk
        model.async_client = sdk.chat.completions
        bound = model.bind_tools([ECHO])
        assistant = await _ainvoke_rate_limited(bound, [first_message])
        await _ainvoke_rate_limited(bound, [first_message, assistant, HumanMessage("continue")])
    assert requests[1]["messages"][1]["reasoning_content"] == "private thought"

    configs["p"] = _cfg("openai-compatible", base_url="https://other.invalid/v1")
    payload = build_model("p")._get_request_payload(
        [first_message, assistant, HumanMessage("continue")]
    )
    assert payload["messages"][1]["content"] == "visible answer"
    assert "reasoning_content" not in payload["messages"][1]
    assert assistant.additional_kwargs["reasoning_content"] == "private thought"
