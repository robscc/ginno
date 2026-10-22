"""Secret 脱敏层单测(ginno-open-dynamic-workflow-comparison P1-②)。

规则语义:
- 键名后缀命中 secret 词族(password/passwd/secret/token/api[_-]?key/auth/
  credential,可带复数 s)→ 值替换 [REDACTED],递归 dict/list;
- 值级兜底只认高置信前缀(sk-/ghp_/gho_/xox*/AKIA/Bearer …);
- 原结构不被修改(返回新树);
- 超深度/超宽度的子树原样保留(卫生层不是校验层)。"""

from __future__ import annotations

import pytest

from ginno_runtime.workflows.secret_redact import (
    is_secret_key,
    redact,
    redact_env,
    redact_text,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "key,hits",
    [
        ("API_KEY", True),
        ("api_key", True),
        ("OPENAI_API_KEY", True),
        ("GITHUB_TOKEN", True),
        ("auth_token", True),
        ("dingtalk_secret", True),
        ("GITHUB_TOKEN", True),
        ("PASSWORD", True),
        ("SECRETS", False),  # 复数不收(宁漏勿错):input_tokens 等用量键是 _tokens 结尾
        ("input_tokens", False),
        ("output_tokens", False),
        ("total_tokens", False),
        ("my-auth", True),
        ("credential", True),
        ("keyboard", False),
        ("authority", False),
        ("token_count", False),
        ("api_key_hash", False),
        ("", False),
        ("x" * 200, False),
    ],
)
def test_is_secret_key(key: str, hits: bool) -> None:
    assert is_secret_key(key) is hits


def test_redact_replaces_by_key_recursively_and_keeps_others() -> None:
    tree = {
        "name": "daily-report",
        "api_key": "sk-should-not-survive",
        "nested": {"AUTH_TOKEN": "t", "plain": "keep", "list": [{"password": "p"}, "x"]},
    }
    out = redact(tree)
    assert out["api_key"] == "[REDACTED]"
    assert out["nested"]["AUTH_TOKEN"] == "[REDACTED]"
    assert out["nested"]["list"][0]["password"] == "[REDACTED]"
    assert out["name"] == "daily-report"
    assert out["nested"]["plain"] == "keep"
    assert out["nested"]["list"][1] == "x"


def test_redact_does_not_mutate_input() -> None:
    tree = {"token": "secret-value"}
    redact(tree)
    assert tree["token"] == "secret-value"


def test_redact_value_level_known_prefixes() -> None:
    text = (
        "leak: sk-ABCDEFGHIJKLMNOPQRSTUVWX2345 and ghp_"
        + "a" * 36
        + " and AKIA1234567890ABCDEF here"
    )
    out = redact_text(text)
    assert "sk-ABCDEF" not in out and "ghp_" not in out and "AKIA1234" not in out
    assert "leak:" in out and "here" in out


def test_redact_value_level_leaves_ordinary_text() -> None:
    assert redact_text("the sky is sk-y today, bearer of news") == "the sky is sk-y today, bearer of news"


def test_redact_depth_cap_keeps_subtree() -> None:
    deep = {"a": None}
    node = deep
    for _ in range(40):
        node["a"] = {"a": None}
        node = node["a"]
    node["a"] = {"token": "x"}
    out = redact(deep)  # 不炸栈;超深子树原样
    assert isinstance(out, dict)


def test_redact_env_masks_secret_names() -> None:
    out = redact_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-x", "HOME": "/u"})
    assert out["ANTHROPIC_API_KEY"] == "[REDACTED]"
    assert out["PATH"] == "/bin" and out["HOME"] == "/u"
