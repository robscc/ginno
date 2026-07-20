"""Unit tests for the permission policy matcher (deny -> ask -> allow -> default ask)."""

from __future__ import annotations

import pytest

from ginno_runtime.permission.policy import PermissionPolicy, _parse_rule

pytestmark = pytest.mark.unit


def _policy(allow=None, deny=None, ask=None):
    return PermissionPolicy.from_settings(
        settings={"permissions": {"allow": allow or [], "deny": deny or [], "ask": ask or []}}
    )


def test_default_decision_is_ask():
    assert _policy().decide("anything", "{}") == "ask"


def test_deny_takes_precedence():
    p = _policy(allow=["Bash(*)"], deny=["Bash(rm -rf *)"], ask=["Bash(*)"])
    assert p.decide("Bash", "rm -rf /") == "deny"


def test_ask_before_allow():
    p = _policy(allow=["Bash(*)"], ask=["Bash(git *)"])
    assert p.decide("Bash", "git push") == "ask"


def test_allow_when_only_allow_matches():
    p = _policy(allow=["read_file"], ask=["write_file"])
    assert p.decide("read_file", "{'path': 'x'}") == "allow"


def test_glob_arg_matching():
    p = _policy(allow=["Bash(git *)"])
    assert p.decide("Bash", "git status") == "allow"
    # non-matching args fall through to default ask
    assert p.decide("Bash", "rm -rf /") == "ask"


def test_tool_name_case_insensitive():
    p = _policy(deny=["bash(sudo *)"])
    assert p.decide("Bash", "sudo reboot") == "deny"


def test_rule_without_parens_matches_any_args():
    r = _parse_rule("write_file")
    assert r.tool == "write_file"
    assert r.arg_pattern == "*"
    assert r.matches("write_file", "anything at all")


def test_rule_with_empty_parens_defaults_to_star():
    r = _parse_rule("Bash()")
    assert r.arg_pattern == "*"


def test_seeded_defaults_behaviour():
    # mirrors paths._DEFAULT_SETTINGS
    p = _policy(
        allow=["read_file", "glob_files"],
        deny=["Bash(rm -rf *)", "bash(sudo *)"],
        ask=["Bash(*)", "write_file", "edit_file"],
    )
    assert p.decide("read_file", "{}") == "allow"
    assert p.decide("write_file", "{}") == "ask"
    assert p.decide("Bash", "rm -rf /tmp") == "deny"
    assert p.decide("Bash", "ls") == "ask"
