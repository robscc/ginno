"""Code Mode 原型单测（选项 B）：stdio RPC 往返 / return-only / 权限 deny / 超时。

全程确定性工具，零真实 LLM。验证执行器骨架机制正确，收益数据见
scripts/code_mode_bench.py。
"""

from ginno_runtime.code_mode import run_repl


def _tools():
    return {
        "echo": lambda a: a.get("x", ""),
        "add": lambda a: a["a"] + a["b"],
        "big": lambda a: "L" * 5000,  # 大中间输出，验证不进 return
    }


def test_rpc_roundtrip_and_return_only():
    code = 'a = tool("add", {"a": 2, "b": 3})\nreturn {"sum": a}'
    r = run_repl(code, _tools())
    assert r.error is None
    assert r.value == {"sum": 5}
    assert r.inner_calls == 1
    # return-only：工具输出只含 return 值
    assert r.to_tool_output() == '{"sum": 5}'


def test_intermediate_stays_in_executor():
    # 大中间结果被代码消化，只有小摘要 return——上下文经济的核心
    code = 'big = tool("big", {})\nreturn {"len": len(big)}'
    r = run_repl(code, _tools())
    assert r.value == {"len": 5000}
    assert "LLLL" not in r.to_tool_output()  # 大输出未回传
    assert r.inner_calls == 1


def test_permission_denied_raises_in_code():
    code = 'tool("echo", {"x": 1})\nreturn "should not reach"'
    r = run_repl(code, _tools(), permission=lambda n, a: "deny")
    assert r.error and "permission deny" in r.error
    assert r.inner_calls == 1


def test_unknown_tool_error():
    r = run_repl('return tool("nope", {})', _tools())
    assert r.error and "unknown tool" in r.error


def test_multi_call_chain():
    code = (
        'x = tool("add", {"a": 1, "b": 1})\n'
        'y = tool("add", {"a": x, "b": 10})\n'
        'return y'
    )
    r = run_repl(code, _tools())
    assert r.value == 12
    assert r.inner_calls == 2
