"""MCP tool arg type fidelity — 2026-11-07 "expected number, received string".

End-to-end symptom: model tool_calls pass native numbers for an MCP tool whose
inputSchema declares ``{"type": "number"}``, yet the server-side validation
rejects the call with ``expected number, received string`` (map server bbox ×4,
reproduced from BOTH desktop and wecom-channel sessions, i.e. the common
upstream).

Root cause pinned by these tests (two halves, both required):
1. ``_schema_to_model`` typed every field ``Any`` → the provider-facing
   tool_call_schema loses the JSON-schema types (``{"title": "West"}`` with no
   ``"type"``). Models — glm on the openai-compatible endpoint observed —
   emit stringly-typed numbers for untyped fields, so the model itself sends
   ``{"west": "139.7449"}``.
2. With ``Any`` fields pydantic coerces nothing, so the string sailed through
   the whole ginno bridge unchanged and hit the server's zod/number check.

The fix maps schema types to real annotations (number→float, integer→int,
boolean→bool, string→str-with-numeric-restore). Then the provider schema
carries the types (model emits numbers natively), and any stringly-typed value
that still arrives is restored to the declared type by pydantic lax validation
BEFORE ``session.call_tool`` — a restore, not a relaxation: invalid values
("abc" for a number) now fail validation and surface as a tool error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ginno_runtime.mcp import registry as reg_mod
from ginno_runtime.mcp.registry import MCPServerConfig

pytestmark = pytest.mark.unit


class _FakeSession:
    """Captures what actually reaches the MCP server via call_tool."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict | None):
        self.calls.append((name, dict(arguments or {})))
        return SimpleNamespace(
            content=[SimpleNamespace(text="ok", model_dump=lambda: {})],
            structuredContent=None,
        )


class _MapTool:
    """Mimics the map server's show-map tool: four required number bbox fields
    plus an optional string title (the exact tool from the incident)."""

    name = "show-map"
    description = "show a map"

    @property
    def inputSchema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "west": {"type": "number", "description": "west longitude"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
                "title": {"type": "string"},
                "zoom": {"type": "integer"},
                "labels": {"type": "boolean"},
            },
            "required": ["west", "south", "east", "north"],
        }


def _wrapped(session: _FakeSession):
    live = reg_mod._LiveServer(
        MCPServerConfig(name="map", transport="stdio", command="x")
    )
    live.session = session
    live.tools = [_MapTool()]
    return live._wrap_tool(_MapTool())


async def test_provider_schema_keeps_json_schema_types():
    """The model-facing tool_call_schema must carry the declared types.

    With every field Any the schema degrades to {"title": "West"} (no type),
    and untyped fields invite the model to emit strings. [regression half 1]
    """
    tool = _wrapped(_FakeSession())
    schema = tool.tool_call_schema.model_json_schema()
    props = schema["properties"]
    assert props["west"].get("type") == "number"
    assert props["zoom"].get("type") == "integer"
    assert props["labels"].get("type") == "boolean"
    assert props["title"].get("type") == "string"
    assert props["west"].get("description") == "west longitude"
    assert set(schema.get("required", [])) == {"west", "south", "east", "north"}


async def test_native_number_args_reach_server_as_numbers():
    """Native numbers from model tool_calls must arrive at call_tool intact
    (invoke exactly the way ToolNode does: full ToolCall dict)."""
    session = _FakeSession()
    tool = _wrapped(session)
    await tool.ainvoke(
        {
            "args": {"west": 139.7449, "south": 35.6582, "east": 139.7459, "north": 35.6589},
            "name": tool.name,
            "id": "call1",
            "type": "tool_call",
        }
    )
    _, payload = session.calls[-1]
    for k in ("west", "south", "east", "north"):
        assert isinstance(payload[k], float), f"{k} should stay a float, got {type(payload[k])}"
    assert payload["west"] == pytest.approx(139.7449)


async def test_stringly_number_args_restored_before_server():
    """The incident payload: model emitted STRINGS for number fields (because
    the untyped schema never said "number"). pydantic lax validation must
    restore them to the declared type before session.call_tool. [regression
    half 2 — this is the exact desktop/wecom reproduction]"""
    session = _FakeSession()
    tool = _wrapped(session)
    await tool.ainvoke(
        {
            "args": {
                "west": "139.7449",
                "south": "35.6582",
                "east": "139.7459",
                "north": "35.6589",
                "zoom": "17",
                "labels": "true",
            },
            "name": tool.name,
            "id": "call2",
            "type": "tool_call",
        }
    )
    _, payload = session.calls[-1]
    assert payload["west"] == pytest.approx(139.7449)
    assert payload["north"] == pytest.approx(35.6589)
    assert payload["zoom"] == 17 and isinstance(payload["zoom"], int)
    assert payload["labels"] is True


async def test_string_field_numeric_output_restored_to_str():
    """Symmetric failure: server declares a string field, model emits a bare
    number → must reach the server as str, not number."""
    session = _FakeSession()
    tool = _wrapped(session)
    await tool.ainvoke(
        {
            "args": {
                "west": 139.7449, "south": 35.6582, "east": 139.7459, "north": 35.6589,
                "title": 12345,
            },
            "name": tool.name,
            "id": "call3",
            "type": "tool_call",
        }
    )
    _, payload = session.calls[-1]
    assert payload["title"] == "12345"


async def test_invalid_number_still_rejected():
    """Restoring types must not loosen validation: "abc" for a number field
    fails pydantic (surfaces to the model as a tool error via
    handle_tool_errors) — never silently forwarded."""
    from pydantic import ValidationError

    tool = _wrapped(_FakeSession())
    with pytest.raises(ValidationError):
        await tool.ainvoke(
            {
                "args": {"west": "abc", "south": 35.6582, "east": 139.7459, "north": 35.6589},
                "name": tool.name,
                "id": "call4",
                "type": "tool_call",
            }
        )


async def test_array_and_object_fields_unchanged():
    """array/object fields stay Any (legacy behavior): the json canonicalization
    hack in _wrap_tool._arun keeps receiving raw values exactly as before —
    this fix deliberately does not touch that path."""
    session = _FakeSession()
    live = reg_mod._LiveServer(
        MCPServerConfig(name="map", transport="stdio", command="x")
    )
    live.session = session

    class _ArrTool:
        name = "pick"
        description = None
        inputSchema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
                "meta": {"type": "object"},
            },
            "required": ["tags"],
        }

    live.tools = [_ArrTool()]
    tool = live._wrap_tool(_ArrTool())
    await tool.ainvoke({"args": {"tags": ["a", "b"], "meta": {"x": 1}}, "name": tool.name, "id": "c", "type": "tool_call"})
    _, payload = session.calls[-1]
    # same output the old Any-typed model produced after the _arun hack
    assert payload["tags"] == '["a","b"]'
    assert payload["meta"] == '{"x":1}'
