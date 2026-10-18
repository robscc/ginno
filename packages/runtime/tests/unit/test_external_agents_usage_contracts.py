"""Usage 归一化契约测试（A4，2026-10-18，借鉴 Multica usage_contract）。

每个后端一组「原始 usage JSON → ginno 口径四元组」的钉死用例。语义：
input_tokens 是**整段提示词**（缓存段并入 input，字段分别落 cache_read /
cache_creation），reasoning 若有则是 output 子集不叠加。CLI 升级导致字段
改名/口径漂移时，在这里显式失败——而不是静默把 0 记进用量账本。
"""
import json

from ginno_runtime.tools.external_agents import (
    ClaudeCodeBackend,
    CodexBackend,
    PiBackend,
)

# 各 CLI 原始 usage 字段的真值样例（来自各 CLI 实测事件流；字段名变了 =
# schema 漂移，先改 fixture 再改解析器，并在本文件注明 CLI 版本）。

CLAUDE_RESULT_EVENT = {
    # claude-code stream-json result 事件（Anthropic 口径：input 不含缓存段）
    "usage": {
        "input_tokens": 100,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 200,
        "output_tokens": 42,
    },
}
PI_MESSAGE_UPDATE = {
    # pi --mode json message_update（json.md v3 字段名）
    "usage": {"input": 100, "output": 42, "cacheRead": 200, "cacheWrite": 30},
}
CODEX_TURN_COMPLETED = {
    # codex exec --json turn.completed（OpenAI Responses 口径：
    # cached_input_tokens 是 input_tokens 的子集）
    "usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 42},
}


def test_claude_usage_contract():
    ev = dict(CLAUDE_RESULT_EVENT, type="result", subtype="success",
              result="ok", is_error=False)
    r = ClaudeCodeBackend().parse_result(json.dumps(ev), "", 0, None)
    assert r.usage == {
        # cache-inclusive：input + cache_read + cache_creation
        "input_tokens": 330,
        "output_tokens": 42,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 30,
    }


def test_pi_usage_contract():
    ev = dict(PI_MESSAGE_UPDATE, type="message_update")
    r = PiBackend().parse_result(
        json.dumps(ev) + '\n{"type":"agent_end","willRetry":false}', "", 0, None
    )
    assert r.usage == {
        "input_tokens": 300,  # input + cacheRead
        "output_tokens": 42,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 30,  # cacheWrite
    }


def test_codex_usage_contract():
    ev = dict(CODEX_TURN_COMPLETED, type="turn.completed")
    r = CodexBackend().parse_result(
        json.dumps(ev), "", 0, None,
    )
    assert r.usage == {
        "input_tokens": 500,  # input_tokens + cached_input_tokens（ginno 全量口径）
        "output_tokens": 42,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 0,  # codex 不报 cache 写入
    }


def test_usage_missing_fields_degrade_to_zero_not_crash():
    ev = '{"type":"result","subtype":"success","result":"ok","is_error":false,"usage":{"input_tokens":5}}'
    r = ClaudeCodeBackend().parse_result(ev, "", 0, None)
    assert r.usage == {
        "input_tokens": 5,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_usage_absent_means_empty_not_zero_dict():
    # 无 usage 事件 → 空 dict（账本跳过），不得伪造 0 记录
    ev = '{"type":"result","subtype":"success","result":"ok","is_error":false}'
    r = ClaudeCodeBackend().parse_result(ev, "", 0, None)
    assert r.usage == {}
