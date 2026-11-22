"""PATCH /api/teams/{id} 的队名校验（R3 巡检 Q4）。

现象：改名只有 UI 输入框挡空串，API 层不挡——直连 PATCH 可以把队名改成
空串/全空白并落库，侧栏、@ 投递菜单、Dock 徽标里出现无名团队行。
修复：显式提供 ``name`` 时服务端校验非空非空白（422 + 明确 error），
落库统一 strip（与建队路径对齐）；不带 name 的 PATCH 不受影响。

回归口径：
1. 置空拒（422，roster 名不变）；
2. 全空白拒（422，同上——strip 后为空）；
3. 正常改名过（200，roster 更新，且存的是 strip 后的名字）；
4. 不传 name 的 PATCH（如 archived）不被误伤。
"""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


def _create_team(client, patch_build_model, name="原队名", slug="default"):
    # 建队走 create_session，需要可用的模型工厂（照 test_team_p0 的范式）。
    patch_build_model(script(text="ok"))
    r = client.post("/api/teams", json={"name": name, "project_slug": slug})
    data = r.json()
    assert r.status_code == 200 and data.get("ok"), data
    return data["team"]


def _roster_name(client, team_id: str) -> str:
    r = client.get(f"/api/teams/{team_id}")
    data = r.json()
    assert r.status_code == 200 and data.get("ok"), data
    return str((data.get("team") or {}).get("name") or "")


def test_patch_empty_name_rejected(client, patch_build_model):
    team = _create_team(client, patch_build_model)
    r = client.patch(f"/api/teams/{team['id']}", json={"name": ""})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body.get("ok") is False
    assert body.get("error"), "422 必须带明确 error 文案"
    # 拒绝不得落库：roster 名保持原值
    assert _roster_name(client, team["id"]) == "原队名"


def test_patch_whitespace_name_rejected(client, patch_build_model):
    team = _create_team(client, patch_build_model)
    r = client.patch(f"/api/teams/{team['id']}", json={"name": "   \t\n "})
    assert r.status_code == 422, r.text
    assert r.json().get("ok") is False
    assert _roster_name(client, team["id"]) == "原队名"


def test_patch_non_string_name_rejected(client, patch_build_model):
    """类型异常也算「置空」：不允许把 name 改成 null 绕过校验。"""
    team = _create_team(client, patch_build_model)
    r = client.patch(f"/api/teams/{team['id']}", json={"name": None})
    assert r.status_code == 422, r.text
    assert _roster_name(client, team["id"]) == "原队名"


def test_patch_valid_name_passes_and_strips(client, patch_build_model):
    team = _create_team(client, patch_build_model)
    r = client.patch(f"/api/teams/{team['id']}", json={"name": "  新队名  "})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok"), data
    # 与建队路径对齐：落库的是 strip 后的名字
    assert _roster_name(client, team["id"]) == "新队名"


def test_patch_without_name_unaffected(client, patch_build_model):
    """不带 name 的 PATCH（归档/换 leader）不被防呆误伤。"""
    team = _create_team(client, patch_build_model)
    r = client.patch(f"/api/teams/{team['id']}", json={"archived": True})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok"), data
    assert data["team"]["archived"] is True
    assert _roster_name(client, team["id"]) == "原队名"
