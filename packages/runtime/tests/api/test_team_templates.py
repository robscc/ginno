"""内置团队模板（api/team_templates.py）单测。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ginno_runtime.api import team_templates as tpl


def _agents(*ids: str):
    return [SimpleNamespace(id=i, name=i) for i in ids]


def test_registry_has_four_templates_with_unique_ids():
    ids = [t["id"] for t in tpl.TEAM_TEMPLATES]
    assert len(ids) == len(set(ids)) == 4
    assert "cabinet" in ids


def test_every_template_has_exactly_one_leader():
    for t in tpl.TEAM_TEMPLATES:
        roles = [m["role"] for m in t["members"]]
        assert roles.count("leader") == 1
        assert 1 < len(t["members"]) <= tpl._MAX_MEMBERS


def test_cabinet_leader_carries_report_contract():
    cab = next(t for t in tpl.TEAM_TEMPLATES if t["id"] == "cabinet")
    leader = next(m for m in cab["members"] if m["role"] == "leader")
    assert "未验证清单" in leader["instructions"]
    assert "已做、卡哪、需要什么" in leader["instructions"]


def test_resolve_picks_hinted_agents(monkeypatch):
    monkeypatch.setattr(tpl, "_resolve_agents", lambda: _agents("main", "explorer", "coder", "reviewer"))
    # 跳过 normalize 的注册表校验（它会把假 agent id 换成真实注册表的 id）
    monkeypatch.setattr(tpl.team_gen_api, "_normalize_spec", lambda s: s)
    out = tpl.resolve_template("cabinet")
    spec = out["spec"]
    by_name = {m["display_name"]: m for m in spec["members"]}
    assert by_name["首辅"]["agent_id"] == "main"
    assert by_name["翰林院编修"]["agent_id"] == "explorer"
    assert by_name["工部尚书"]["agent_id"] == "coder"
    assert by_name["大理寺卿"]["agent_id"] == "reviewer"
    # 归一化保证恰好一个 leader
    assert [m["role"] for m in spec["members"]].count("leader") == 1


def test_resolve_falls_back_round_robin_when_no_hints_hit(monkeypatch):
    monkeypatch.setattr(tpl, "_resolve_agents", lambda: _agents("only-agent"))
    monkeypatch.setattr(tpl.team_gen_api, "_normalize_spec", lambda s: s)
    out = tpl.resolve_template("research")
    # 单 agent 安装：全员落到同一 agent（多实例语义）
    assert all(m["agent_id"] == "only-agent" for m in out["spec"]["members"])


def test_resolve_unknown_template_404():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        tpl.resolve_template("nope")
    assert ei.value.status_code == 404


def test_resolve_no_agents_400(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(tpl, "_resolve_agents", lambda: [])
    with pytest.raises(HTTPException) as ei:
        tpl.resolve_template("cabinet")
    assert ei.value.status_code == 400


def test_endpoints_shape(monkeypatch):
    import asyncio

    monkeypatch.setattr(tpl, "_resolve_agents", lambda: _agents("main", "reviewer"))
    listing = asyncio.run(tpl.list_templates())
    assert listing["ok"] and any(t["id"] == "cabinet" for t in listing["templates"])
    preview = asyncio.run(tpl.preview_template("cabinet"))
    assert preview["ok"] and preview["spec"]["members"]
