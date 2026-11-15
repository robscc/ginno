"""api_key 的 env:VAR 引用（2026-11-16 安全加固）。

动机：headless node 以非 root 用户跑，入站 bash 不受 exec_root 约束，
同用户可读的 settings.json 里的明文 api_key 可被远端派活读走。
支持 env 引用后密钥可放进 root-only 的 systemd EnvironmentFile。

关键防线：**只在构建模型时解析**，配置读取层不得解析——否则 settings GET
会把明文回给前端，等于换个地方泄露。
"""

from __future__ import annotations

import pytest

from ginno_runtime.models import resolve_api_key


def test_env_ref_resolves(monkeypatch):
    monkeypatch.setenv("GINNO_TEST_KEY", "sk-real-secret")
    assert resolve_api_key("env:GINNO_TEST_KEY") == "sk-real-secret"


def test_plain_key_passthrough():
    assert resolve_api_key("sk-plain") == "sk-plain"


def test_empty_stays_empty():
    assert resolve_api_key("") == ""
    assert resolve_api_key(None) == ""


def test_missing_env_fails_loud(monkeypatch):
    monkeypatch.delenv("GINNO_TEST_ABSENT", raising=False)
    with pytest.raises(ValueError, match="未设置或为空"):
        resolve_api_key("env:GINNO_TEST_ABSENT")


def test_empty_env_fails_loud(monkeypatch):
    monkeypatch.setenv("GINNO_TEST_EMPTY", "")
    with pytest.raises(ValueError, match="未设置或为空"):
        resolve_api_key("env:GINNO_TEST_EMPTY")


def test_bare_env_prefix_fails_loud():
    with pytest.raises(ValueError, match="缺少变量名"):
        resolve_api_key("env:")


def test_env_ref_with_spaces_trimmed(monkeypatch):
    monkeypatch.setenv("GINNO_TEST_SP", "sk-x")
    assert resolve_api_key("env:  GINNO_TEST_SP  ") == "sk-x"


def test_config_read_layer_does_not_resolve(monkeypatch):
    """配置读取层必须保留 env: 引用原文，绝不解析成明文。

    这是防泄露的关键：settings GET 走读取层，若在此解析就把密钥送给了前端。
    """
    from ginno_runtime import providers

    monkeypatch.setenv("GINNO_TEST_LEAK", "sk-must-not-leak")
    cfgs = providers.normalize_config(
        {
            "id": "t",
            "protocol": "openai",
            "api_key": "env:GINNO_TEST_LEAK",
            "default_model": "gpt-4o",
        }
    )
    assert cfgs["api_key"] == "env:GINNO_TEST_LEAK", "读取层不得解析 env 引用"


def test_build_model_resolves_env_ref_on_real_path(monkeypatch):
    """build_model 真路径上解析 env 引用——用「缺失即抛 env 错」证明，不靠空断言。

    若解析没接进 build_model，缺 env 时会走到后续「key 为空」之类别的错误，
    错误消息不同 → 本用例红。故它能真正钉住接线，不是假绿。
    """
    import ginno_runtime.models as m
    from ginno_runtime import providers as prov_mod

    monkeypatch.delenv("GINNO_TEST_BM_ABSENT", raising=False)
    monkeypatch.delenv("GINNO_FAKE_LLM", raising=False)
    cfg = {
        "id": "p",
        "protocol": "openai",
        "api_key": "env:GINNO_TEST_BM_ABSENT",
        "default_model": "gpt-4o",
        "base_url": "https://example.invalid/v1",
        "enabled": True,
        "max_tokens": 100,
        "temperature": 0.5,
        "timeout_s": 30,
    }
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"p": cfg})
    with pytest.raises(ValueError, match="未设置或为空"):
        m.build_model("p")


def test_build_model_env_ref_present_passes_key_check(monkeypatch):
    """env 有值时不得再报「key 为空/env 未设置」——证明解析出了真值。"""
    import ginno_runtime.models as m
    from ginno_runtime import providers as prov_mod

    monkeypatch.setenv("GINNO_TEST_BM_OK", "sk-built")
    monkeypatch.delenv("GINNO_FAKE_LLM", raising=False)
    cfg = {
        "id": "p",
        "protocol": "openai",
        "api_key": "env:GINNO_TEST_BM_OK",
        "default_model": "gpt-4o",
        "base_url": "https://example.invalid/v1",
        "enabled": True,
        "max_tokens": 100,
        "temperature": 0.5,
        "timeout_s": 30,
    }
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"p": cfg})
    try:
        m.build_model("p")
    except ValueError as exc:
        # 允许后续 SDK/网络层报错，但绝不得是 key 解析/空值类错误
        assert "未设置或为空" not in str(exc), f"env 解析未生效: {exc}"
        assert "API key" not in str(exc) or "empty" not in str(exc).lower(), (
            f"key 未被解析成真值: {exc}"
        )
