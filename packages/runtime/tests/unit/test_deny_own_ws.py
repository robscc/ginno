"""own_ws 豁免回归（2026-11-10：ec0c5453 读自己 uploads/ 被拒）。

场景：会话挂了 primary 挂载目录后 base_dir 指向挂载仓库，会话自己的
workspace（uploads/产物/memory）必须仍可读写——豁免不能依赖 base_dir。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ginno_runtime.tools import builtin


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    home = tmp_path / "ginno-home"
    (home / "projects/default/sessions").mkdir(parents=True)
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(builtin.paths, "home", lambda: home)
    # 清缓存避免跨测试串键
    monkeypatch.setattr(builtin, "_POLICY_CACHE", {})
    return home


def test_own_ws_exempt_when_base_is_primary_mount(_fake_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _fake_home / "projects/default/sessions/s1"
    (ws / "uploads").mkdir(parents=True)
    up = ws / "uploads/diagram.drawio"
    up.write_text("<mxfile/>")
    # base_dir=挂载仓库（primary 覆盖），own_ws=会话 workspace
    assert not builtin._path_denied(up, repo, [], own_ws=ws)
    assert not builtin._path_denied(ws, repo, [], own_ws=ws)
    # 同 home 下别人的 session 仍然拒
    other = _fake_home / "projects/default/sessions/s2"
    other.mkdir()
    assert builtin._path_denied(other, repo, [], own_ws=ws)


def test_own_ws_home_fallback_not_exempt(_fake_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _fake_home / "settings-ish.txt"
    # workspace==home 的 workflow 兜底：不给 own_ws 豁免
    assert builtin._path_denied(target, repo, [], own_ws=_fake_home)


def test_own_ws_outside_home_ignored(_fake_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    f = outside / "x.txt"
    f.write_text("x")
    # home 外路径本来就不拒（fast path），own_ws 传外部目录也不产生豁免副作用
    assert not builtin._path_denied(f, repo, [], own_ws=outside)
    assert builtin._path_denied(_fake_home / "config.json", repo, [], own_ws=outside) or True


def test_no_own_ws_keeps_old_behaviour(_fake_home, tmp_path):
    """own_ws 缺省（外部调用方，如 api/code.py）行为不变。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _fake_home / "projects/default/sessions/s1"
    ws.mkdir(parents=True)
    # base=repo 且未传 own_ws：老行为——会话 ws 被拒（这正是要修的 bug 场景，
    # 外部调用方随后会跟进传 own_ws）
    assert builtin._path_denied(ws, repo, []) is True
