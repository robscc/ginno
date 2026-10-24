"""External-agent adapters（一个 CLI 一个文件）的解析与 argv 矩阵测试。

fixture 锁定各 CLI 事件 schema 的关键字段（pi JSONL v3 / claude
stream-json / codex --json），字段漂移在这里显式失败。
"""
from ginno_runtime import jsonx as json
from ginno_runtime.tools.external_agents import (
    ClaudeCodeBackend,
    CodexBackend,
    PiBackend,
)

PI_JSONL = "\n".join([
    '{"type":"session","version":3,"id":"s1","cwd":"/tmp"}',
    '{"type":"message_update","usage":{"input":100,"output":5,"cacheRead":10,"cacheWrite":2,"totalTokens":117}}',
    '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Reading a.py"}]}}',
    '{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"t1","name":"read","arguments":{"path":"a.py"}}]}}',
    '{"type":"tool_execution_end","toolCallId":"t1","toolName":"read","result":"print(1)","isError":false}',
    '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}',
    '{"type":"agent_end","willRetry":false}',
    # 坏行/空行必须被跳过
    "not json at all",
    "",
])

CLAUDE_STREAM = "\n".join([
    '{"type":"system","subtype":"init","session_id":"c1","model":"claude-x"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"will read"},{"type":"tool_use","id":"u1","name":"Read","input":{"path":"a.py"}}]}}',
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"u1","content":"print(1)","is_error":false}]}}',
    '{"type":"result","subtype":"success","result":"ok","is_error":false,"usage":{"input_tokens":10,"output_tokens":2,"cache_read_input_tokens":3,"cache_creation_input_tokens":1},"num_turns":1,"duration_ms":123,"cost_usd":0.001,"session_id":"c1"}',
])

CLAUDE_LEGACY = (
    '{"result":"old","is_error":false,"usage":{"input_tokens":5,"output_tokens":1},'
    '"session_id":"z","num_turns":1,"duration_ms":9,"cost_usd":0.5,"model":"claude-code"}'
)

CODEX_JSONL = "\n".join([
    '{"type":"thread.started","thread_id":"t1"}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"thinking aloud"}}',
    '{"type":"item.completed","item":{"type":"command_execution","command":"ls","aggregated_output":"a.py","status":"completed"}}',
    '{"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":2,"output_tokens":3}}',
])


def test_pi_happy_path():
    r = PiBackend().parse_result(PI_JSONL, "", 0, None)
    assert r.stop_reason == "success"
    assert r.output == "done"  # 最后一条 assistant message_end 的文本
    assert r.meta["session_id"] == "s1"
    assert r.usage == {
        "input_tokens": 110,  # input + cacheRead（ginno 归一化）
        "output_tokens": 5,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 2,
    }
    # 3 assistant + 1 tool_result（坏行被跳过）
    assert [e["role"] for e in r.transcript] == ["assistant", "assistant", "tool_result", "assistant"]
    assert r.transcript[1]["tool_calls"][0]["name"] == "read"
    assert r.transcript[2]["id"] == "t1"


def test_pi_retry_failure():
    bad = '{"type":"auto_retry_end","success":false,"finalError":"boom"}'
    r = PiBackend().parse_result(bad, "", 0, None)
    assert r.stop_reason == "error" and "boom" in r.diagnostic


def test_claude_stream_json():
    r = ClaudeCodeBackend().parse_result(CLAUDE_STREAM, "", 0, None)
    assert r.stop_reason == "success" and r.output == "ok"
    assert r.usage["input_tokens"] == 14  # 10 + 3 + 1（cache 并入 input）
    assert r.meta["cost_usd"] == 0.001 and r.meta["num_turns"] == 1
    assert [e["role"] for e in r.transcript] == ["assistant", "tool_result"]
    assert r.transcript[0]["tool_calls"][0]["id"] == "u1"


def test_claude_legacy_fallback():
    r = ClaudeCodeBackend().parse_result(CLAUDE_LEGACY, "", 0, None)
    assert r.output == "old" and r.stop_reason == "success" and r.transcript == []


def test_codex_jsonl_without_last_message_file():
    r = CodexBackend().parse_result(CODEX_JSONL, "", 0, None)
    assert r.stop_reason == "success"
    assert r.output == "thinking aloud"  # 无 out_file → 最后一条 agent_message
    assert r.usage["input_tokens"] == 9  # 7 + 2 cached
    assert [e["role"] for e in r.transcript] == ["assistant", "tool_result"]
    assert r.transcript[1]["name"] == "bash"


def test_argv_matrices():
    ro = PiBackend().build_argv("P", "read-only", "/w", None)
    assert ro[ro.index("--tools") + 1] == "read,grep,find,ls"  # 无 bash/edit/write
    ed = PiBackend().build_argv("P", "edit", "/w", None)
    # 2026-10-05 用户决策：edit 不传 --tools，走 pi 默认全部内置工具（含 bash）
    assert "--tools" not in ed
    assert "--no-session" in ro and "--no-extensions" in ro and "--mode" in ro

    c = ClaudeCodeBackend().build_argv("P", "read-only", "/w", None)
    assert "stream-json" in c and "--verbose" in c
    # 2026-10-05 用户决策：完整工具表 + --dangerously-skip-permissions，
    # permissions.deny/--settings 已移除（headless 无人应答权限提示）。
    assert "--dangerously-skip-permissions" in c
    assert not any(a.startswith("--settings=") for a in c)
    c2 = ClaudeCodeBackend().build_argv("P", "edit", "/w", None)
    assert "--dangerously-skip-permissions" in c2
    assert "--permission-mode=acceptEdits" in c2

    x = CodexBackend().build_argv("P", "read-only", "/w", "/tmp/out.txt")
    assert "--json" in x and "--output-last-message" in x and "-s" in x


# ---- 边界 case（2026-10-04 加固轮）----

def test_pi_empty_and_garbage_stdout():
    r = PiBackend().parse_result("", "", 0, None)
    assert r.stop_reason == "error" and "no assistant output" in r.diagnostic
    r2 = PiBackend().parse_result("total garbage\n\n{broken", "boom", 1, None)
    assert r2.stop_reason == "error" and "rc=1" in r2.diagnostic


def test_claude_result_is_error_and_rc_nonzero():
    evs = '{"type":"result","subtype":"error_during_execution","result":"partial","is_error":true,"usage":{"input_tokens":1,"output_tokens":1}}'
    r = ClaudeCodeBackend().parse_result(evs, "", 0, None)
    assert r.stop_reason == "error" and r.output == "partial" and r.usage
    r2 = ClaudeCodeBackend().parse_result(evs, "crash", 3, None)
    assert r2.stop_reason == "error" and "rc=3" in r2.diagnostic


# ---- fail-closed 终态判定（A1，2026-10-18）：无 result 事件 ≠ 成功 ----

def _truncated_stream(last_text: str) -> str:
    """有 assistant 文本但流戛然而止（无 result 事件）的形态。"""
    return "\n".join([
        '{"type":"system","subtype":"init","session_id":"c1","model":"claude-x"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}',
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": last_text}]},
        }),
    ])


def test_claude_truncated_stream_salvages_with_note():
    r = ClaudeCodeBackend().parse_result(_truncated_stream("almost done"), "", 0, None)
    assert r.stop_reason == "success" and r.output == "working\nalmost done"
    assert r.meta["terminal_reason"] == "stream_truncated"
    assert "without a result event" in r.diagnostic  # [note] 通道带出


def test_claude_truncated_context_exhausted_is_error():
    r = ClaudeCodeBackend().parse_result(
        _truncated_stream("prompt is too long: 250000 tokens > 200000"), "", 0, None
    )
    assert r.stop_reason == "error"
    assert r.meta["terminal_reason"] == "context_exhausted"
    assert "context exhausted" in r.diagnostic

    r2 = ClaudeCodeBackend().parse_result(
        _truncated_stream("the context window limit was reached mid-task"), "boom", 1, None
    )
    assert r2.stop_reason == "error" and r2.meta["terminal_reason"] == "context_exhausted"

    # stderr 里的指纹也算（CLI 级报错不一定进 assistant 文本）
    r3 = ClaudeCodeBackend().parse_result(
        _truncated_stream("continuing"), "request too large: 413", 1, None
    )
    assert r3.stop_reason == "error" and r3.meta["terminal_reason"] == "context_exhausted"


def test_claude_truncated_without_text_is_error():
    # 全是 tool_use 中途断掉：无可抢救，报 error 而非空成功
    evs = "\n".join([
        '{"type":"system","subtype":"init","session_id":"c1"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"u1","name":"Read","input":{}}]}}',
    ])
    r = ClaudeCodeBackend().parse_result(evs, "killed", 0, None)
    assert r.stop_reason == "error"
    assert r.meta["terminal_reason"] == "stream_truncated"
    assert "without any assistant text" in r.diagnostic


def test_claude_normal_result_not_flagged():
    # 正常收尾流里模型讨论到上下文措辞不算（指纹只作用于 result 缺席）
    evs = CLAUDE_STREAM.replace('"result":"ok"', '"result":"the context window is fine"')
    r = ClaudeCodeBackend().parse_result(evs, "", 0, None)
    assert r.stop_reason == "success" and "terminal_reason" not in r.meta


def test_unicode_and_multiline_survive():
    text = "完成 🎉\nline2 中文"
    jl = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    r = PiBackend().parse_result(jl, "", 0, None)
    assert r.output == text and r.transcript[0]["text"] == text


def test_orphan_and_multi_tool_calls_pairing():
    from langchain_core.messages import AIMessage, ToolMessage

    from ginno_runtime.delegation_sessions import _ir_to_messages

    ir = [
        {"role": "assistant", "text": "two calls", "tool_calls": [
            {"id": "a", "name": "read", "args": {}},
            {"id": "b", "name": "grep", "args": {}},
        ]},
        {"role": "tool_result", "id": "a", "name": "read", "content": "1", "is_error": False},
        {"role": "tool_result", "id": "b", "name": "grep", "content": "2", "is_error": False},
        {"role": "tool_result", "id": None, "name": "bash", "content": "ls", "is_error": False},
    ]
    msgs = _ir_to_messages(ir, "p")
    ai = next(m for m in msgs if isinstance(m, AIMessage) and m.tool_calls)
    assert [c["id"] for c in ai.tool_calls] == ["a", "b"]
    paired = [m for m in msgs if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in paired} >= {"a", "b"} and len(paired) == 3
    # 无 id 的孤儿（codex 形态）也能配对（合成 id）
    assert paired[2].name == "bash"
