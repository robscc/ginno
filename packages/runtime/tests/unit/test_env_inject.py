"""env_inject（bash 工具环境变量注入）单测。

覆盖：settings 缺失 / 无 env 键 / 坏 JSON / 非字典 / 值类型归一 /
PATH 追加语义 / build_child_env 继承降级。
"""

import json

import pytest

from ginno_runtime.tools import env_inject


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(env_inject, "settings_path", lambda: tmp_path / "settings.json")


def _write(d, data):
    (d / "settings.json").write_text(json.dumps(data) if data is not None else "{bad")


def test_missing_settings_returns_empty(tmp_path):
    assert env_inject.load_extra_env() == {}


def test_no_env_key_returns_empty(tmp_path):
    _write(tmp_path, {"rtk": {"enabled": False}})
    assert env_inject.load_extra_env() == {}


def test_bad_json_degrades_to_empty(tmp_path):
    (tmp_path / "settings.json").write_text("{oops")
    assert env_inject.load_extra_env() == {}


def test_non_dict_env_degrades_to_empty(tmp_path):
    _write(tmp_path, {"env": ["HTTP_PROXY=x"]})
    assert env_inject.load_extra_env() == {}


def test_value_type_normalization(tmp_path):
    _write(tmp_path, {"env": {"A": "s", "B": 42, "C": 1.5, "D": True, "": "skip"}})
    # 注：settings.json 走 JSON，空字符串键 "" 被过滤，其余字符串键放行。
    assert env_inject.load_extra_env() == {
        "A": "s", "B": "42", "C": "1.5", "D": "true",
    }


def test_build_child_env_none_when_no_extra(tmp_path):
    assert env_inject.build_child_env() is None


def test_build_child_env_merges(monkeypatch, tmp_path):
    _write(tmp_path, {"env": {"HTTPS_PROXY": "http://127.0.0.1:7890"}})
    monkeypatch.setattr(env_inject.os, "environ", {"HOME": "/u/me"})
    env = env_inject.build_child_env()
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert env["HOME"] == "/u/me"


def test_build_child_env_path_appends_not_replaces(monkeypatch, tmp_path):
    _write(tmp_path, {"env": {"PATH": "/opt/tools/bin"}})
    monkeypatch.setattr(env_inject.os, "environ", {"PATH": "/usr/bin:/bin"})
    env = env_inject.build_child_env()
    assert env["PATH"] == "/usr/bin:/bin:/opt/tools/bin"


def test_empty_env_dict_means_inherit(tmp_path):
    _write(tmp_path, {"env": {}})
    assert env_inject.build_child_env() is None
