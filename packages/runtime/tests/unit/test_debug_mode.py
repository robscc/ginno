"""Debug 模式开关（``ginno_runtime.debug``）单元测试。

契约：
- 默认关闭；关闭时浏览器面整体熄灭（``browse`` skill 不对外暴露）。
- 开发/测试可用环境变量强开：``GINNO_DEBUG=1`` 或任一 ``GINNO_BROWSER_ENGINE``
  值（conftest 的 ``isolated_home`` 恒设 ``GINNO_BROWSER_ENGINE=fake``，
  因此整套 ``test_browser_*`` 不受影响）。
- ``settings.json`` 的 ``"debug": true`` 开启。
- 进程级单次求值（重启生效）：首次调用后缓存，改 settings.json 不影响，
  直到 ``reset_debug_cache()``（仅测试隔离用）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime import debug
from ginno_runtime.skills.loader import load_all_skills


@pytest.fixture(autouse=True)
def fresh_debug_cache():
    """每个用例前后清空进程级缓存，防止求值顺序串味。"""
    debug.reset_debug_cache()
    yield
    debug.reset_debug_cache()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GINNO_DEBUG", raising=False)
    monkeypatch.delenv("GINNO_BROWSER_ENGINE", raising=False)


def _write_settings(home: Path, payload: dict) -> None:
    home.joinpath("settings.json").write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 求值规则
# --------------------------------------------------------------------------- #
def test_env_ginno_debug_forces_on(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GINNO_DEBUG", "1")
    assert debug.debug_enabled() is True


def test_env_browser_engine_forces_on(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    """conftest 依赖此行为：设了 GINNO_BROWSER_ENGINE=fake 即视为调试环境。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("GINNO_BROWSER_ENGINE", "fake")
    assert debug.debug_enabled() is True


def test_default_off_without_settings(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    assert debug.debug_enabled() is False


def test_settings_debug_true_turns_on(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    _write_settings(isolated_home, {"debug": True})
    assert debug.debug_enabled() is True


def test_settings_debug_false_stays_off(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    _write_settings(isolated_home, {"debug": False})
    assert debug.debug_enabled() is False


def test_settings_malformed_json_stays_off(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    isolated_home.joinpath("settings.json").write_text("{not json", encoding="utf-8")
    assert debug.debug_enabled() is False


# --------------------------------------------------------------------------- #
# 进程级缓存（重启生效）
# --------------------------------------------------------------------------- #
def test_cached_until_reset(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    assert debug.debug_enabled() is False  # 首次求值并缓存
    _write_settings(isolated_home, {"debug": True})
    assert debug.debug_enabled() is False  # 缓存不变：改文件不热生效
    debug.reset_debug_cache()
    assert debug.debug_enabled() is True  # 相当于「重启」后重新读盘


# --------------------------------------------------------------------------- #
# 下游守卫取样：browse skill 随开关显隐
# --------------------------------------------------------------------------- #
def test_browse_skill_hidden_when_off(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    names = {s.name for s in load_all_skills()}
    assert "browse" not in names
    assert "todo" in names  # 非浏览器 skill 不受影响


def test_browse_skill_present_when_on(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    _clear_env(monkeypatch)
    _write_settings(isolated_home, {"debug": True})
    names = {s.name for s in load_all_skills()}
    assert "browse" in names
