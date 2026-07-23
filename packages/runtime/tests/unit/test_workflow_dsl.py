"""Unit tests for the workflow DSL schema + projections (pure, no io)."""

from __future__ import annotations

from ginno_runtime.workflows import dsl


def _valid() -> dict:
    return {
        "name": "w",
        "entry": "a",
        "nodes": [
            {"id": "a", "type": "step", "goal": "do a", "agent": "dev"},
            {"id": "b", "type": "step", "goal": "do b"},
        ],
        "edges": [{"from": "a", "to": "b"}],
    }


def test_validate_accepts_valid_dsl():
    assert dsl.validate_dsl(dsl.normalize_dsl(_valid())) == []


def test_validate_missing_entry():
    d = dsl.normalize_dsl(_valid())
    d["entry"] = "zzz"
    errs = dsl.validate_dsl(d)
    assert any("entry" in e for e in errs)


def test_validate_unknown_edge_endpoint():
    d = dsl.normalize_dsl(_valid())
    d["edges"].append({"from": "a", "to": "ghost"})
    errs = dsl.validate_dsl(d)
    assert any("ghost" in e for e in errs)


def test_validate_branch_needs_cases_or_default():
    d = dsl.normalize_dsl(_valid())
    d["nodes"].append({"id": "br", "type": "branch"})
    assert any("branch 'br'" in e for e in dsl.validate_dsl(d))


def test_validate_loop_needs_max_iters_and_body():
    d = dsl.normalize_dsl(_valid())
    d["nodes"].append({"id": "lp", "type": "loop", "over": "context.x"})
    errs = dsl.validate_dsl(d)
    assert any("loop 'lp'" in e and "body" in e for e in errs)
    assert any("loop 'lp'" in e and "max_iters" in e for e in errs)


def test_validate_subflow_rejected_in_v1():
    d = dsl.normalize_dsl(_valid())
    d["nodes"].append({"id": "sf", "type": "subflow"})
    assert any("subflow" in e for e in dsl.validate_dsl(d))


def test_legacy_steps_to_dsl_builds_linear_chain():
    d = dsl.legacy_steps_to_dsl(
        [{"id": "s1", "title": "one", "agent_id": "dev"}, {"title": "two"}],
        name="n",
        description="d",
    )
    d = dsl.normalize_dsl(d)
    assert dsl.validate_dsl(d) == []
    assert [n["id"] for n in d["nodes"]] == ["s1", "s2"]
    assert d["nodes"][0]["type"] == "step"
    assert d["nodes"][0]["agent"] == "dev"
    assert d["nodes"][0]["goal"] == "one"
    assert d["edges"] == [{"from": "s1", "to": "s2"}]
    assert d["entry"] == "s1"
    assert d["name"] == "n"


def test_steps_from_dsl_projects_title_and_agent():
    d = dsl.normalize_dsl(_valid())
    steps = dsl.steps_from_dsl(d)
    assert steps[0] == {"id": "a", "title": "do a", "agent_id": "dev"}
    assert steps[1]["agent_id"] is None
    assert steps[1]["title"] == "do b"


def test_normalize_fills_context_and_version():
    d = dsl.normalize_dsl({"nodes": [{"type": "step", "goal": "x"}]})
    assert d["dsl_version"] == "1"
    assert d["context"]["schema"]["type"] == "object"
    assert d["context"]["initial"] == {}
    assert d["nodes"][0]["id"]  # auto-assigned


def test_roundtrip_legacy_then_project_is_stable():
    d = dsl.legacy_steps_to_dsl([{"title": "a"}, {"title": "b"}])
    assert dsl.steps_from_dsl(dsl.normalize_dsl(d)) == [
        {"id": "s1", "title": "a", "agent_id": None},
        {"id": "s2", "title": "b", "agent_id": None},
    ]


def test_normalize_defaults_supervisor():
    d = dsl.normalize_dsl(_valid())
    assert d["supervisor"] == {"enabled": False, "mode": "human"}


def test_validate_supervisor_mode_when_enabled():
    d = dsl.normalize_dsl(_valid())
    d["supervisor"] = {"enabled": True, "mode": "bogus"}
    assert any("supervisor.mode" in e for e in dsl.validate_dsl(d))
    d["supervisor"] = {"enabled": True, "mode": "human"}
    assert dsl.validate_dsl(d) == []
    d["supervisor"] = {"enabled": False, "mode": "bogus"}  # disabled -> mode not enforced
    assert dsl.validate_dsl(d) == []
