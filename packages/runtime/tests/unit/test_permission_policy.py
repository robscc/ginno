"""Unit tests for the permission policy matcher (deny -> ask -> allow -> default ask)."""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
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


def test_relay_answer_seeded_allow():
    """relay_answer 默认 allow（2026-11-21 用户决策：qid 凭证链已是强门，
    再套 ask 等于二次确认用户刚做的决定）。fresh seed 在
    paths._DEFAULT_SETTINGS，升级 install 由 ensure_relay_permissions 迁移。"""
    # 1) fresh-install 默认表里有它
    from ginno_runtime import paths as _paths

    allow = _paths._DEFAULT_SETTINGS["permissions"]["allow"]
    assert "relay_answer" in allow

    # 2) 升级迁移：已有 settings 无该名 → 幂等补入 allow
    from ginno_runtime.permission.policy import ensure_relay_permissions

    sp = paths.settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"permissions": {"allow": ["read_file"],
                                               "ask": ["bash(*)"]}}))
    ensure_relay_permissions()
    migrated = json.loads(sp.read_text())["permissions"]["allow"]
    assert "relay_answer" in migrated
    # 幂等：再跑不重复
    ensure_relay_permissions()
    assert json.loads(sp.read_text())["permissions"]["allow"].count("relay_answer") == 1

    # 3) 用户显式 ask/deny 优先（不覆盖用户意志）
    sp.write_text(json.dumps({"permissions": {"allow": [],
                                              "ask": ["relay_answer"]}}))
    ensure_relay_permissions()
    assert json.loads(sp.read_text())["permissions"]["ask"] == ["relay_answer"]
    assert json.loads(sp.read_text())["permissions"]["allow"] == []
