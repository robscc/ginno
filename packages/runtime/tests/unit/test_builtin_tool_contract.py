"""Pin the model-facing contract captured before the builtin registry split."""

import json
from pathlib import Path

import pytest

from ginno_runtime.tools import build_builtin_tools

pytestmark = pytest.mark.unit


def _assert_contract(tools):
    expected = json.loads(
        Path(__file__).with_name("data").joinpath("builtin_tools_golden.json").read_text(
            encoding="utf-8"
        )
    )
    actual = [
        {
            "name": tool.name,
            "description": tool.description,
            "schema": tool.get_input_schema().model_json_schema(),
        }
        for tool in tools
    ]
    assert [entry["name"] for entry in actual] == [entry["name"] for entry in expected]
    assert actual == expected


def test_builtin_contract_without_session():
    _assert_contract(build_builtin_tools())


def test_builtin_contract_with_session_and_mount(tmp_path):
    _assert_contract(build_builtin_tools(
        workspace=str(tmp_path / "workspace"),
        context_dirs=[{"path": str(tmp_path), "access": "ro"}],
        primary_path=str(tmp_path),
        session_id="session",
    ))


@pytest.mark.parametrize(
    ("session_id", "depth", "restricted", "expected"),
    [
        (None, None, False, ["read_file", "glob_files"]),
        ("session", 0, False, ["read_file", "glob_files", "spawn_subagent"]),
        ("session", 2, True, ["read_file", "glob_files"]),
    ],
)
def test_graph_builtin_registration(session_id, depth, restricted, expected):
    from ginno_runtime.graph import build_all_tools

    relevant = ["read_file", "glob_files", "spawn_subagent"]
    tools = build_all_tools(
        project_slug="default",
        session_id=session_id,
        subagent_depth=depth,
        restrict_tools=relevant if restricted else None,
    )
    assert [tool.name for tool in tools if tool.name in relevant] == expected
    if restricted:
        assert [tool.name for tool in tools] == expected
    else:
        _assert_contract(tools[:6])
