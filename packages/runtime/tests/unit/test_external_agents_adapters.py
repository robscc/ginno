"""External-agent adapters（一个 CLI 一个文件）的解析与 argv 矩阵测试。

fixture 锁定各 CLI 事件 schema 的关键字段（pi JSONL v3 / claude
stream-json / codex --json / dsh NDJSON），字段漂移在这里显式失败。
"""
from ginno_runtime import jsonx as json
from ginno_runtime.tools.external_agents import (
    ClaudeCodeBackend,
    CodexBackend,
    DshBackend,
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


# --------------------------------------------------------------------------- #
# dsh（`dsh --profile headless --json` NDJSON 事件流）
# --------------------------------------------------------------------------- #
# 形状照 /tmp/dsh-fixtures 的真实流裁短（去掉真实路径/会话号）：夹杂一条
# 非 JSON 诊断行、工具循环、usage 逐 step 上报。
DSH_SUCCESS_STREAM = "\n".join([
    '{"type":"session","sessionId":"session-dsh-1","cwd":"/tmp/ws"}',
    '{"type":"status","phase":"turn_start","turn":1}',
    '{"type":"status","phase":"step_start","turn":1,"step":1}',
    "[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)",
    '{"type":"thinking","text":"先看看目录"}',
    '{"type":"tool_call","callId":"call_a","tool":"bash","input":{"command":"ls"}}',
    '{"type":"tool_result","callId":"call_a","status":"completed","result":"a.py"}',
    '{"type":"status","phase":"step_end","turn":1,"step":1,"usage":{"inputTokens":100,"outputTokens":10,"cacheReadTokens":20,"cacheWriteTokens":5,"totalTokens":135}}',
    '{"type":"status","phase":"step_start","turn":1,"step":2}',
    '{"type":"thinking","text":""}',
    '{"type":"text","text":"Reading a.py"}',
    '{"type":"tool_call","callId":"call_b","tool":"read","input":{"path":"a.py"}}',
    '{"type":"tool_result","callId":"call_b","status":"error","result":"Error: not found"}',
    '{"type":"status","phase":"step_end","turn":1,"step":2,"usage":{"inputTokens":50,"outputTokens":20,"cacheReadTokens":30}}',
    '{"type":"status","phase":"step_start","turn":1,"step":3}',
    '{"type":"text","text":"all done"}',
    '{"type":"status","phase":"step_end","turn":1,"step":3,"usage":{"inputTokens":10,"outputTokens":5}}',
    '{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"completed"}}',
    '{"type":"final","text":"all done"}',
])

# 续跑（Lead 实测 0.2.1-rc.3）：sessionId 不变、turn_start turn=2（会话级
# 轮次递增），本次调用仍然只跑了 1 轮 → num_turns=1。
DSH_RESUME_STREAM = "\n".join([
    '{"type":"session","sessionId":"session-dsh-1","cwd":"/tmp/ws"}',
    '{"type":"status","phase":"turn_start","turn":2}',
    '{"type":"status","phase":"step_start","turn":2,"step":1}',
    '{"type":"text","text":"42"}',
    '{"type":"status","phase":"step_end","turn":2,"step":1,"usage":{"inputTokens":554,"outputTokens":58,"cacheReadTokens":5376,"cacheWriteTokens":0,"totalTokens":5988}}',
    '{"type":"status","phase":"turn_end","turn":2,"reason":{"kind":"completed"}}',
    '{"type":"final","text":"42"}',
])

# 失败流：turn_end.reason.error + 空 final（真实样例 dsh-error.stream.jsonl）。
DSH_ERROR_STREAM = "\n".join([
    '{"type":"session","sessionId":"session-dsh-err","cwd":"/tmp/ws"}',
    '{"type":"status","phase":"turn_start","turn":1}',
    '{"type":"status","phase":"step_start","turn":1,"step":1}',
    '{"type":"status","phase":"step_end","turn":1,"step":1}',
    '{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"error","error":{"message":"llm-deepseek: no API key for provider route \\"deepseek-official\\"","code":"MISSING_CREDENTIAL"}}}',
    '{"type":"final","text":""}',
])

DSH_ERROR_STDERR = (
    'dsh: MISSING_CREDENTIAL: llm-deepseek: no API key for provider route '
    '"deepseek-official"'
)

# 未知 session id（Lead 实测）：rc=1，只有 runner 级 error 事件，没有 final。
DSH_UNKNOWN_SESSION_STREAM = (
    '{"type":"error","message":"session \\"session-nope\\" does not exist; '
    'omit --session-id to start a new Session"}'
)


def test_dsh_argv_and_env_matrix(monkeypatch):
    """argv：--profile headless --json 固定；续跑才追加 --session-id（在
    prompt 前）；权限模式永远不进 argv，只走 DSH_PERMISSION_MODE。"""
    from ginno_runtime.tools.external_agents import base as _xa_base

    monkeypatch.setitem(_xa_base._CLI_CACHE, "dsh", "/opt/dsh")
    b = DshBackend()
    argv = b.build_argv("P", "read-only", "/w", None)
    assert argv == ["/opt/dsh", "--profile", "headless", "--json", "P"]
    # 权限模式不在 argv（dsh 只认环境变量）——edit 的 argv 与 read-only 相同
    assert b.build_argv("P", "edit", "/w", None) == argv
    assert "--session-id" not in argv
    resumed = b.build_argv("P", "read-only", "/w", None, "session-abc")
    assert resumed == [
        "/opt/dsh", "--profile", "headless", "--json",
        "--session-id", "session-abc", "P",
    ]
    assert resumed.index("--session-id") < len(resumed) - 1  # 在 prompt 前
    assert b.build_env("read-only", "/w") == {"DSH_PERMISSION_MODE": "read-only"}
    assert b.build_env("edit", "/w") == {"DSH_PERMISSION_MODE": "workspace-write"}
    # 续跑标记 + 设置页引导码
    assert b.supports_resume is True and b.setup_hint_code == "needsAuth"


def test_session_id_ignored_by_non_resumable_adapters(monkeypatch):
    """claude/codex/pi 的 build_argv 接受 session_id 但忽略（argv 不变）。"""
    for be in (ClaudeCodeBackend(), CodexBackend(), PiBackend()):
        plain = be.build_argv("P", "read-only", "/w", None)
        with_sid = be.build_argv("P", "read-only", "/w", None, "session-abc")
        assert with_sid == plain
        assert "--session-id" not in with_sid


def test_dsh_success_stream():
    r = DshBackend().parse_result(DSH_SUCCESS_STREAM, "", 0, None)
    assert r.stop_reason == "success"
    assert r.output == "all done"  # 权威通道 = final
    assert r.meta["session_id"] == "session-dsh-1"
    assert r.meta["model"] == "dsh"
    assert r.meta["num_turns"] == 1
    assert r.diagnostic == ""
    # 非 JSON 诊断行被跳过；thinking 不进转录；空 text 跳过
    assert [e["role"] for e in r.transcript] == [
        "assistant", "tool_result", "assistant", "assistant",
        "tool_result", "assistant",
    ]
    assert r.transcript[0]["tool_calls"][0]["name"] == "bash"
    assert r.transcript[0]["tool_calls"][0]["args"] == {"command": "ls"}
    # tool_result 没有工具名 → 用 callId 从 tool_call 反查
    assert r.transcript[1]["id"] == "call_a" and r.transcript[1]["name"] == "bash"
    assert r.transcript[1]["content"] == "a.py" and r.transcript[1]["is_error"] is False
    assert r.transcript[2] == {"role": "assistant", "text": "Reading a.py", "tool_calls": []}
    assert r.transcript[4]["name"] == "read" and r.transcript[4]["is_error"] is True
    # usage 逐 step_end 累加：input 含缓存段（ginno 全量口径）
    assert r.usage == {
        "input_tokens": 160 + 50 + 5,
        "output_tokens": 10 + 20 + 5,
        "cache_read_tokens": 50,
        "cache_creation_tokens": 5,
    }


def test_dsh_resume_stream_keeps_session_and_counts_one_turn():
    """续跑流：sessionId 复用、turn 从 2 起，但 num_turns 按本次调用=1。"""
    r = DshBackend().parse_result(DSH_RESUME_STREAM, "", 0, None)
    assert r.stop_reason == "success" and r.output == "42"
    assert r.meta["session_id"] == "session-dsh-1"
    assert r.meta["num_turns"] == 1
    assert r.usage == {
        "input_tokens": 554 + 5376,
        "output_tokens": 58,
        "cache_read_tokens": 5376,
        "cache_creation_tokens": 0,
    }


def test_dsh_error_stream_uses_turn_end_reason():
    r = DshBackend().parse_result(DSH_ERROR_STREAM, DSH_ERROR_STDERR, 1, None)
    assert r.stop_reason == "error"
    # 诊断优先 turn_end.reason.error.message（带 code）
    assert "MISSING_CREDENTIAL" in r.diagnostic
    assert "no API key for provider route" in r.diagnostic
    assert r.meta["error_code"] == "MISSING_CREDENTIAL"
    assert r.meta["terminal_reason"] == "error"
    # 失败流没有正文：空 final + 无 text → output 留空（stdout 尾部是事件流/
    # 诊断行，不能当答复回给模型；诊断已含 code+message）。
    assert r.output == ""
    assert r.usage == {}


def test_dsh_unknown_session_error_event():
    """未知/失效 session id：DSH 自己报错（error 事件）→ stop=error，id
    原样留在诊断里（不吞掉，模型/用户看得到）。"""
    r = DshBackend().parse_result(DSH_UNKNOWN_SESSION_STREAM, "", 1, None)
    assert r.stop_reason == "error"
    assert "session-nope" in r.diagnostic and "does not exist" in r.diagnostic
    assert r.meta["terminal_reason"] == "error"


def test_dsh_rc_nonzero_is_error_even_with_completed_turn():
    r = DshBackend().parse_result(DSH_SUCCESS_STREAM, "crash", 2, None)
    assert r.stop_reason == "error" and "rc=2" in r.diagnostic
    assert r.output == "all done"  # 答复照带，账照记


def test_dsh_final_empty_falls_back_to_last_text():
    stream = "\n".join([
        '{"type":"status","phase":"turn_start","turn":1}',
        '{"type":"text","text":"intermediate"}',
        '{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"completed"}}',
        '{"type":"final","text":""}',
    ])
    r = DshBackend().parse_result(stream, "", 0, None)
    assert r.stop_reason == "success" and r.output == "intermediate"


def test_dsh_truncated_stream_without_turn_end_is_error():
    """fail-closed：没等到显式 completed 的 turn_end 不算成功。"""
    stream = '{"type":"session","sessionId":"s"}\n{"type":"text","text":"half"}'
    r = DshBackend().parse_result(stream, "killed", 0, None)
    assert r.stop_reason == "error"
    assert "turn_end" in r.diagnostic
    assert r.output == "half"


def test_dsh_aborted_turn_reason_surfaces():
    stream = "\n".join([
        '{"type":"status","phase":"turn_start","turn":1}',
        '{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"aborted","reason":"user cancelled"}}',
        '{"type":"final","text":"partial"}',
    ])
    r = DshBackend().parse_result(stream, "", 1, None)
    assert r.stop_reason == "error"
    assert "user cancelled" in r.diagnostic
    assert r.meta["terminal_reason"] == "aborted"


def test_dsh_configured_matrix(monkeypatch, tmp_path):
    """configured()：env / $DSH_HOME/.env / profile patch 三来源；DSH_HOME
    隔离生效；任何读失败都降级为 False（绝不抛）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    b = DshBackend()
    assert b.configured() is False  # 什么都没有

    # 1) 启动环境变量
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert b.configured() is True
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert b.configured() is False

    # 2) $DSH_HOME/.env（支持 export 前缀与引号；空值不算）
    env_file = dsh_home / ".env"
    env_file.write_text("OTHER=1\nDEEPSEEK_API_KEY=\n")
    assert b.configured() is False
    env_file.write_text("# comment\nexport DEEPSEEK_API_KEY='sk-file'\n")
    assert b.configured() is True

    # 3) headless profile patch 里的账号路由
    env_file.unlink()
    patch = dsh_home / "profiles" / "headless" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    patch.write_text("llm:\n  provider: deepseek-account\n")
    assert b.configured() is True
    patch.write_text("llm:\n  provider: other\n")
    assert b.configured() is False

    # 读失败（DSH_HOME 是个文件）→ False，不抛
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x")
    monkeypatch.setenv("DSH_HOME", str(bogus))
    assert b.configured() is False


def test_dsh_configured_default_home_when_dsh_home_unset(monkeypatch, tmp_path):
    """DSH_HOME 未设 → 回落到 ~/.dsh（HOME 驱动）。"""
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert DshBackend().configured() is False
    (tmp_path / ".dsh").mkdir()
    (tmp_path / ".dsh" / ".env").write_text("DEEPSEEK_API_KEY=sk-home\n")
    assert DshBackend().configured() is True
