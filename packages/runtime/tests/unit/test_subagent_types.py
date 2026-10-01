"""Unit tests for the subagent type registry (P3 contract 1).

Coverage: frontmatter parsing + tolerance (bad YAML / missing or malformed
name / non-dict frontmatter / duplicates → skip + warn, never raise), the
dir-state cache (edits land on the next read), tools_allow normalization
(list vs comma string), describe_types routing list, restrict_for_meta, and
the model resolution rule (provider/model wins only for a KNOWN config id).
"""

from __future__ import annotations

import json
import logging

import pytest

from ginno_runtime import subagent_types as st

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_cache():
    st._CACHE = None
    st._DIR_STATE = None
    yield
    st._CACHE = None
    st._DIR_STATE = None


def _write(name: str, content: str) -> None:
    d = st.subagents_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


def _valid(name: str = "researcher", body: str = "你是只读调研员。", **fm) -> str:
    meta = {"name": name, "description": "只读调研类型", **fm}
    lines = ["---"]
    lines += [f"{k}: {v}" for k, v in meta.items()]
    lines += ["---", "", body, ""]
    return "\n".join(lines)


def test_loads_valid_type_with_body_and_allow(isolated_home):
    _write(
        "researcher.md",
        _valid(tools_allow="[read_file, grep_files]", model="custom/gpt-x"),
    )
    types = st.load_subagent_types()
    t = types["researcher"]
    assert t.description == "只读调研类型"
    assert t.tools_allow == ["read_file", "grep_files"]
    assert t.model == "custom/gpt-x"
    assert t.body == "你是只读调研员。"
    assert st.get_subagent_type("Researcher") is t  # case-insensitive lookup


def test_comma_string_tools_allow(isolated_home):
    _write("a.md", _valid("alpha", tools_allow="read_file, glob_*"))
    assert st.get_subagent_type("alpha").tools_allow == ["read_file", "glob_*"]


def test_bad_yaml_frontmatter_skipped_with_warning(isolated_home, caplog):
    _write("broken.md", "---\n: : [unclosed\n---\nbody\n")
    with caplog.at_level(logging.WARNING, logger="ginno_runtime.subagent_types"):
        types = st.load_subagent_types()
    assert types == {}
    assert any("subagent_type" in r.getMessage() for r in caplog.records)


def test_missing_or_bad_name_skipped(isolated_home, caplog):
    _write("noname.md", "---\ndescription: 没有名字\n---\nbody\n")
    _write("badname.md", _valid("Has Space"))
    with caplog.at_level(logging.WARNING, logger="ginno_runtime.subagent_types"):
        types = st.load_subagent_types()
    assert types == {}


def test_non_dict_frontmatter_skipped(isolated_home):
    _write("list.md", "---\n- a\n- b\n---\nbody\n")
    assert st.load_subagent_types() == {}


def test_duplicate_name_keeps_first_sorted_file(isolated_home, caplog):
    _write("aaa.md", _valid("dup") + "\n(first)")
    _write("zzz.md", _valid("dup") + "\n(second)")
    with caplog.at_level(logging.WARNING, logger="ginno_runtime.subagent_types"):
        types = st.load_subagent_types()
    assert "(first)" in types["dup"].body and "(second)" not in types["dup"].body
    assert any("duplicate" in r.getMessage() for r in caplog.records)


def test_dir_state_cache_picks_up_edits(isolated_home):
    _write("r.md", _valid("researcher", body="v1"))
    assert st.get_subagent_type("researcher").body == "v1"
    # same-name rewrite (mtime changes) → next read sees v2
    import time as _t

    _t.sleep(0.01)
    _write("r.md", _valid("researcher", body="v2"))
    assert st.get_subagent_type("researcher").body == "v2"
    # a new file lands too
    _write("w.md", _valid("writer"))
    assert st.get_subagent_type("writer") is not None
    # an unknown dir reads as an empty registry, never raises
    assert st.get_subagent_type("nope") is None


def test_describe_types_sorted_for_routing(isolated_home):
    _write("b.md", _valid("beta", ))
    _write("a.md", _valid("alpha"))
    rows = st.describe_types()
    assert [r["name"] for r in rows] == ["alpha", "beta"]
    assert rows[0]["description"] == "只读调研类型"


def test_restrict_for_meta(isolated_home):
    _write("r.md", _valid("researcher", tools_allow="[read_file]"))
    assert st.restrict_for_meta(
        {"subagent": {"agent_type": "researcher"}}
    ) == ["read_file"]
    # legacy meta (no agent_type) and unknown type → no restriction
    assert st.restrict_for_meta({"subagent": {}}) == []
    assert st.restrict_for_meta({"subagent": {"agent_type": "ghost"}}) == []
    assert st.restrict_for_meta(None) == []


def test_resolve_type_model_known_provider_wins(isolated_home, monkeypatch):
    from ginno_runtime import providers as prov_mod

    monkeypatch.setattr(
        prov_mod, "load_configs", lambda *a, **k: [{"id": "prov1", "name": "P"}]
    )
    t = st.SubagentType(name="t", description="", model="prov1/gpt-x")
    assert st.resolve_type_model(t, ("custom", "base")) == ("prov1", "gpt-x")
    # unknown provider prefix → whole string rides the parent's provider
    t2 = st.SubagentType(name="t", description="", model="ghost/m")
    assert st.resolve_type_model(t2, ("custom", "base")) == ("custom", "ghost/m")
    # bare model name → parent provider
    t3 = st.SubagentType(name="t", description="", model="gpt-x")
    assert st.resolve_type_model(t3, ("custom", "base")) == ("custom", "gpt-x")
    # empty model → untouched inheritance
    t4 = st.SubagentType(name="t", description="", model="")
    assert st.resolve_type_model(t4, ("custom", "base")) == ("custom", "base")


def test_missing_dir_is_empty_registry(isolated_home):
    assert st.load_subagent_types() == {}
    assert st.describe_types() == []


def test_settings_file_shape_unrelated(isolated_home):
    """The registry reads agents/subagents/*.md only — an unrelated
    settings.json must not confuse it (sanity for the shared GINNO_HOME)."""
    p = st.paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"subagent": {"max_concurrent": 3}}), encoding="utf-8")
    assert st.load_subagent_types() == {}
