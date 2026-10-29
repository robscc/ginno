"""Unit tests for the ask_user tool, with LangGraph's interrupt() monkeypatched
to a fixed resume value (so the tool body is tested without a live graph/WS).
The interrupt -> event -> WS -> resume path is covered by
tests/e2e/test_ask_user_flow.py."""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.tools import ask_tools as at

pytestmark = pytest.mark.unit


def _tool():
    tools = {t.name: t for t in at.build_ask_tools("default", "sess-1")}
    return tools["ask_user"]


def _invoke(tool, args: dict, tcid: str = "tc-1") -> str:
    """InjectedToolCallId requires the full model ToolCall shape, not bare args.

    That form makes invoke() return a ToolMessage, so unwrap its content —
    which is what the graph actually stores and the model actually reads.
    """
    out = tool.invoke({"name": "ask_user", "args": args, "id": tcid, "type": "tool_call"})
    return out.content if hasattr(out, "content") else out


def test_needs_a_session():
    """No session = no socket to answer over (workflow engine, listings)."""
    assert at.build_ask_tools("default", None) == []


def test_schema_hides_the_injected_id():
    t = _tool()
    props = t.tool_call_schema.model_json_schema()["properties"]
    assert "tool_call_id" not in props
    assert set(props) == {"question", "options", "header", "allow_free_text", "questions"}


def test_option_choice(isolated_home, monkeypatch):
    seen = {}

    def _fake(value):
        seen.update(value)
        return {"kind": "user_answer", "answer": "装到仓库", "option_index": 1}

    monkeypatch.setattr(at, "interrupt", _fake)
    out = json.loads(
        _invoke(_tool(), {
                "question": "装到哪？",
                "options": ["Ginno 全局", "装到仓库"],
                "header": "选择安装位置",
            })
    )
    # The interrupt payload is what the server turns into the WS event.
    assert seen["kind"] == "user_question"
    assert seen["options"] == ["Ginno 全局", "装到仓库"]
    assert seen["header"] == "选择安装位置"
    assert out["ok"] is True and out["skipped"] is False
    assert out["source"] == "option" and out["option_index"] == 1
    assert out["answer"] == "装到仓库" and out["free_text"] == ""


def test_free_text(isolated_home, monkeypatch):
    monkeypatch.setattr(
        at, "interrupt", lambda v: {"answer": "装到 /tmp/whatever", "option_index": None}
    )
    out = json.loads(
        _invoke(_tool(), {"question": "装到哪？", "options": ["a", "b"]})
    )
    assert out["source"] == "free_text" and out["answer"] == "装到 /tmp/whatever"
    assert out["option_index"] is None


def test_out_of_range_index_falls_back_to_free_text(isolated_home, monkeypatch):
    """A stale option_index (client raced a re-emit) must not IndexError."""
    monkeypatch.setattr(
        at, "interrupt", lambda v: {"answer": "typed", "option_index": 9}
    )
    out = json.loads(_invoke(_tool(), {"question": "q", "options": ["a"]}))
    assert out["source"] == "free_text" and out["answer"] == "typed"


def test_skip(isolated_home, monkeypatch):
    monkeypatch.setattr(at, "interrupt", lambda v: {"skip": True})
    out = json.loads(_invoke(_tool(), {"question": "q", "options": ["a"]}))
    assert out["ok"] is True and out["skipped"] is True and out["source"] == "skipped"


def test_missing_question(isolated_home):
    assert _invoke(_tool(), {"question": "   "}).startswith("[error]")


# ---- 多问题（契约 v2，2026-10-29 e48be8a3 实测：3 问塞 1 题 + 组合套餐选项）---- #


def test_multi_payload_and_receipt(isolated_home, monkeypatch):
    """questions≥2：payload 带 multi/questions 逐题字段；回执逐题出结果。"""
    seen = {}

    def _fake(value):
        seen.update(value)
        return {
            "kind": "user_answer",
            "multi": True,
            "answers": [
                {"answer": "", "option_index": 1, "skip": False},  # 选项
                {"answer": "逐题引导", "option_index": None, "skip": False},  # 自由文本
                {"answer": "", "skip": True},  # 单题跳过
            ],
        }

    monkeypatch.setattr(at, "interrupt", _fake)
    out = json.loads(
        _invoke(_tool(), {
                "question": "确认三件事：",
                "header": "Skill 设计确认",
                "questions": [
                    {"question": "产出形态？", "options": ["补全 prd.md", "只产清单"]},
                    {"question": "交互方式？", "options": ["逐题引导", "批量清单"]},
                    {"question": "覆盖范围？"},
                ],
            })
    )
    assert seen["multi"] is True and len(seen["questions"]) == 3
    assert seen["questions"][0]["options"] == ["补全 prd.md", "只产清单"]
    assert out["multi"] is True and len(out["results"]) == 3
    assert out["results"][0]["source"] == "option" and out["results"][0]["answer"] == "只产清单"
    assert out["results"][1]["source"] == "free_text" and out["results"][1]["answer"] == "逐题引导"
    assert out["results"][2]["source"] == "skipped" and out["results"][2]["answer"] == ""


def test_multi_whole_card_skip(isolated_home, monkeypatch):
    monkeypatch.setattr(at, "interrupt", lambda v: {"skip": True})
    out = json.loads(
        _invoke(_tool(), {"questions": [{"question": "a?"}, {"question": "b?"}]})
    )
    assert out["multi"] and all(r["source"] == "skipped" for r in out["results"])


def test_multi_legacy_single_answer_degrades_to_first(isolated_home, monkeypatch):
    """外部渠道/旧客户端按单答形状回复：打在第一题，其余记 skipped。"""
    monkeypatch.setattr(
        at, "interrupt", lambda v: {"answer": "装到仓库", "option_index": None, "skip": False}
    )
    out = json.loads(
        _invoke(_tool(), {
                "questions": [
                    {"question": "a?", "options": ["x", "y"]},
                    {"question": "b?"},
                ],
            })
    )
    assert out["results"][0]["source"] == "free_text" and out["results"][0]["answer"] == "装到仓库"
    assert out["results"][1]["source"] == "skipped"


def test_multi_single_entry_folds_back_to_legacy(isolated_home, monkeypatch):
    """questions 恰 1 条 → 折回单问题旧契约（payload/回执零变化）。"""
    seen = {}

    def _fake(value):
        seen.update(value)
        return {"answer": "y", "option_index": 1}

    monkeypatch.setattr(at, "interrupt", _fake)
    out = json.loads(
        _invoke(_tool(), {
                "questions": [{"question": "选哪个？", "options": ["x", "y"], "header": "h"}],
            })
    )
    assert "multi" not in seen and "questions" not in seen
    assert seen["question"] == "选哪个？" and seen["options"] == ["x", "y"]
    assert out["source"] == "option" and out["answer"] == "y" and "multi" not in out


def test_multi_caps_at_five(isolated_home, monkeypatch):
    monkeypatch.setattr(at, "interrupt", lambda v: {})
    out = _invoke(_tool(), {"questions": [{"question": f"q{i}?"} for i in range(6)]})
    assert out.startswith("[error]") and "5" in out


def test_headless_turn_refuses_without_interrupting(isolated_home, monkeypatch):
    """A goal continuation has nobody to answer — parking there would hang the
    turn until the stop signal."""
    called = []
    monkeypatch.setattr(at, "interrupt", lambda v: called.append(v))
    tok = at.set_interactive(False)
    try:
        out = _invoke(_tool(), {"question": "q", "options": ["a"]})
    finally:
        at.reset_interactive(tok)
    assert out.startswith("[error]") and "unattended" in out
    assert called == []


def test_budget_caps_asks_per_turn(isolated_home, monkeypatch, tmp_path):
    """Beyond the cap the tool refuses instead of parking again."""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps({"context": {"ask_user_max_per_turn": 2}}), encoding="utf-8"
    )
    monkeypatch.setattr(at, "interrupt", lambda v: {"skip": True})
    tok = at.begin_ask_budget()
    try:
        tool = _tool()
        assert json.loads(_invoke(tool, {"question": "1"}))["ok"] is True
        assert json.loads(_invoke(tool, {"question": "2"}))["ok"] is True
        third = _invoke(tool, {"question": "3"})
        assert third.startswith("[error]") and "(limit 2)" in third
    finally:
        at.reset_ask_budget(tok)


def test_budget_is_per_turn_not_global(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps({"context": {"ask_user_max_per_turn": 1}}), encoding="utf-8"
    )
    monkeypatch.setattr(at, "interrupt", lambda v: {"skip": True})
    tool = _tool()
    tok = at.begin_ask_budget()
    at.reset_ask_budget(tok)
    # A fresh turn re-arms the budget — the previous turn's use must not count.
    tok = at.begin_ask_budget()
    try:
        assert json.loads(_invoke(tool, {"question": "q"}))["ok"] is True
    finally:
        at.reset_ask_budget(tok)


def test_unarmed_budget_never_refuses(isolated_home, monkeypatch):
    """No armed budget (a direct tool call outside a turn) = no cap."""
    monkeypatch.setattr(at, "interrupt", lambda v: {"skip": True})
    tool = _tool()
    for _ in range(5):
        assert json.loads(_invoke(tool, {"question": "q"}))["ok"] is True

# --------------------------------------------------------------------------- #
# options normalization — models JSON-encode array args
# --------------------------------------------------------------------------- #
def test_stringified_options_are_parsed_not_iterated(isolated_home, monkeypatch):
    """The 2026-09-20 failure (turn aa49c475): the model sent `options` as the
    STRING '["a", "b"]', pydantic rejected it, and the model burned its whole
    ask budget on retries. Anything that iterates the string renders one option
    per character."""
    seen = {}

    def _fake(value):
        seen.update(value)
        return {"skip": True}

    monkeypatch.setattr(at, "interrupt", _fake)
    out = _invoke(_tool(), {
        "question": "装哪？",
        "options": '["装进当前仓库 .claude/skills", "装进 Ginno 全局"]',
    })
    assert json.loads(out)["ok"] is True
    assert seen["options"] == ["装进当前仓库 .claude/skills", "装进 Ginno 全局"]


def test_a_bare_string_is_one_option(isolated_home, monkeypatch):
    seen = {}
    monkeypatch.setattr(at, "interrupt", lambda v: seen.update(v) or {"skip": True})
    _invoke(_tool(), {"question": "q", "options": "就装到这个仓库"})
    assert seen["options"] == ["就装到这个仓库"]


def test_malformed_array_string_yields_no_garbage(isolated_home, monkeypatch):
    """Better an empty list (a free-text question) than per-character options."""
    seen = {}
    monkeypatch.setattr(at, "interrupt", lambda v: seen.update(v) or {"skip": True})
    _invoke(_tool(), {"question": "q", "options": '["未闭合'})
    assert seen["options"] == []


def test_option_index_still_maps_to_the_parsed_list(isolated_home, monkeypatch):
    """The resume payload's index must index the PARSED list, or the receipt
    names the wrong label."""
    monkeypatch.setattr(
        at, "interrupt", lambda v: {"answer": "b", "option_index": 1}
    )
    out = json.loads(
        _invoke(_tool(), {"question": "q", "options": '["a", "b"]'})
    )
    assert out["source"] == "option" and out["answer"] == "b"


def test_normalize_options_accepts_lists_and_rejects_junk():
    assert at.normalize_options(None) == []
    assert at.normalize_options([" a ", "", "b"]) == ["a", "b"]
    assert at.normalize_options("[1, 2]") == ["1", "2"]
    assert at.normalize_options(123) == []
    # A non-array string is one bare label, JSON-looking or not.
    assert at.normalize_options('{"a": 1}') == ['{"a": 1}']
    assert at.normalize_options('["a", "b"') == []  # malformed array → nothing


def test_normalize_options_unwraps_rich_option_objects():
    """2026-10-29 live: the model sent options as ``[{key, label, description}]``
    and the card's buttons rendered the whole dict literal."""
    raw = [
        {"description": "递归抽取整棵页面树", "key": "tree", "label": "整棵页面树"},
        {"description": "只迁当前页", "key": "single", "label": "仅这一页"},
        {"description": "no label key"},
        {"key": "bark"},  # no label-like key at all → keep the repr
    ]
    assert at.normalize_options(raw) == [
        "整棵页面树",
        "仅这一页",
        "no label key",
        "{'key': 'bark'}",
    ]


def test_normalize_options_unwraps_json_encoded_object_arrays():
    encoded = json.dumps([
        {"description": "标准做法", "key": "tree", "label": "整棵页面树"},
        {"description": "只迁当前页", "key": "single", "label": "仅这一页"},
    ])
    assert at.normalize_options(encoded) == ["整棵页面树", "仅这一页"]


def test_normalize_options_unwraps_dict_shaped_string_entries():
    """Sometimes each entry is itself a stringified object (Python repr or a
    JS-style literal with unquoted keys)."""
    assert at.normalize_options(
        ["{'description': 'x', 'key': 'paste', 'label': '我直接粘贴'}"]
    ) == ["我直接粘贴"]
    assert at.normalize_options(
        ["{description: 'x', key: 'paste', label: '我直接粘贴'}"]
    ) == ["我直接粘贴"]
    # A brace-led string with no label key stays verbatim.
    assert at.normalize_options(["{}"]) == ["{}"]


def test_multi_questions_unwrap_object_options_end_to_end(isolated_home, monkeypatch):
    seen = {}

    def _fake(value):
        seen.update(value)
        return {
            "multi": True,
            "answers": [
                {"option_index": 0, "answer": "整棵页面树"},
                {"option_index": None, "answer": "先摸规模", "skip": False},
            ],
        }

    monkeypatch.setattr(at, "interrupt", _fake)
    out = _invoke(_tool(), {
        "questions": [
            {"question": "范围？", "options": [
                {"description": "递归", "key": "tree", "label": "整棵页面树"},
                {"description": "单页", "key": "single", "label": "仅这一页"},
            ]},
            {"question": "怎么来？"},
        ],
    })
    assert seen["questions"][0]["options"] == ["整棵页面树", "仅这一页"]
    assert json.loads(out)["results"][0]["answer"] == "整棵页面树"
