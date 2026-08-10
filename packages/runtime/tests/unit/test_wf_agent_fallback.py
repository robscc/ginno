"""Agent fallback for workflow DSLs referencing agents that don't exist.

2026-08-10 incident: an LLM-drafted sample DSL invented role names
('research-planner', 'web-researcher', …) and the run died at fork time
(`fork_agent` ValueError) before the first node. The engine now substitutes
a default persona with a visible warning; doctor flags the drafting mistake
statically. These tests pin both layers.
"""

from __future__ import annotations

import pytest

from ginno_runtime import agents as agents_reg
from ginno_runtime import paths
from ginno_runtime.workflows.doctor import run_doctor
from ginno_runtime.workflows.nodes import agent_helpers as ah

pytestmark = pytest.mark.unit


def test_resolve_agent_exact_match_no_warning():
    agents_reg.list_agents()  # triggers ensure_seeded
    agent, warn = ah.resolve_agent("research")
    assert agent is not None and agent.id == "research"
    assert warn is None


def test_resolve_agent_missing_falls_back_to_dev():
    agents_reg.list_agents()  # triggers ensure_seeded
    agent, warn = ah.resolve_agent("research-planner")
    assert agent is not None
    assert agent.id == "dev"  # first entry of _FALLBACK_PREFERENCE
    assert warn and "research-planner" in warn and "dev" in warn


def test_resolve_agent_prefers_first_fallback_that_exists():
    agents_reg.list_agents()  # triggers ensure_seeded
    agents_reg.delete_agent("dev")
    agent, warn = ah.resolve_agent("ghost")
    assert agent is not None and agent.id == "research"
    assert warn and "research" in warn


def test_resolve_agent_no_agents_at_all():
    # A non-json file blocks ensure_seeded without contributing an agent,
    # leaving the registry genuinely empty.
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    (paths.agents_dir() / "broken.json").write_text("{not json")
    agent, warn = ah.resolve_agent("ghost")
    assert agent is None
    assert warn and "ghost" in warn


def test_doctor_flags_missing_agent_reference():
    agents_reg.list_agents()  # triggers ensure_seeded
    dsl = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "step", "goal": "plan", "agent": "research-planner"},
            {"id": "s2", "type": "step", "goal": "do", "agent": "dev"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }
    result = run_doctor(dsl)
    findings = [w for w in result["warnings"] if w["rule"] == "agent.not_found"]
    assert len(findings) == 1
    assert findings[0]["node_id"] == "s1"
    assert "research-planner" in findings[0]["message"]
    # existing agent (dev) produces no finding
    assert all(f["node_id"] != "s2" for f in findings)


def test_doctor_no_agent_field_no_finding():
    agents_reg.list_agents()  # triggers ensure_seeded
    dsl = {
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "goal": "plan"}],
        "edges": [],
    }
    result = run_doctor(dsl)
    assert not [w for w in result["warnings"] if w["rule"] == "agent.not_found"]
