"""Per-agent connector denial (connector-module-design.md §8).

Semantics under test:
- denylist only — an agent with connectors_deny=[] keeps every tool;
- a tool is denied only when ALL connectors providing it (dual-track
  browser: chrome-extension + browser-profile share the browser_ prefix)
  are denied — either track alone keeps the capability alive;
- the denial is a HARD restriction: it beats tools_allow "*" and skill
  extra_allow;
- non-connector tools are never affected.
"""

from __future__ import annotations

import pytest

from ginno_runtime.connectors.builtin import ensure_builtin_connectors
from ginno_runtime.connectors.registry import Connector, ConnectorRegistry
from ginno_runtime.graph import tool_allowed

pytestmark = pytest.mark.unit


def _agent(allow=None, deny=None):
    from ginno_runtime.agents.registry import AgentConfig

    return AgentConfig(
        id="t", name="T", system_prompt="You are T.",
        tools_allow=allow if allow is not None else ["*"],
        connectors_deny=deny or [],
    )


# ---- registry.tool_denied (pure) -------------------------------------------

class _Plain(Connector):
    id = "plain"
    name = "Plain"
    tool_prefix = None


class _TrackA(Connector):
    id = "track-a"
    name = "A"
    tool_prefix = "x_"


class _TrackB(Connector):
    id = "track-b"
    name = "B"
    tool_prefix = "x_"


class _Solo(Connector):
    id = "solo"
    name = "Solo"
    tool_prefix = "solo_"


def _reg() -> ConnectorRegistry:
    reg = ConnectorRegistry()
    reg.register(_Plain())
    reg.register(_TrackA())
    reg.register(_TrackB())
    reg.register(_Solo())
    return reg


def test_tool_denied_unknown_tool_is_never_denied():
    assert _reg().tool_denied("bash", ["track-a", "track-b"]) is False


def test_tool_denied_single_provider_denial():
    reg = _reg()
    assert reg.tool_denied("solo_open", ["solo"]) is True
    assert reg.tool_denied("solo_open", []) is False


def test_tool_denied_dual_track_needs_both():
    reg = _reg()
    assert reg.tool_denied("x_open", ["track-a"]) is False  # B still provides it
    assert reg.tool_denied("x_open", ["track-a", "track-b"]) is True


def test_tool_denied_prefix_not_infix():
    assert _reg().tool_denied("notx_open", ["track-a", "track-b"]) is False


# ---- tool_allowed integration (real builtin registry) ----------------------

def test_browser_deny_beats_wildcard_and_skills():
    ensure_builtin_connectors()  # chrome-extension + browser-profile, prefix browser_
    a = _agent(deny=["chrome-extension", "browser-profile"])
    assert tool_allowed(a, "browser_navigate") is False
    # HARD restriction: skills' extra_allow must not widen past it.
    assert tool_allowed(a, "browser_navigate", ["browser_*"]) is False


def test_browser_dual_track_either_survives():
    ensure_builtin_connectors()
    a = _agent(deny=["chrome-extension"])  # profile track still allowed
    # browser_* binds only through skill activation (extra_allow — the lazy
    # browser gate in tool_allowed); with only the chrome-extension track
    # denied the tool must STILL bind: the profile track survives.
    assert tool_allowed(a, "browser_navigate", ["browser_*"]) is True


def test_browser_deny_spares_other_tools():
    ensure_builtin_connectors()
    a = _agent(deny=["chrome-extension", "browser-profile"])
    assert tool_allowed(a, "bash") is True
    assert tool_allowed(a, "ask_user") is True
