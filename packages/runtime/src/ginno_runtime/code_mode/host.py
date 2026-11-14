"""Code Mode 宿主侧（原型）——见 docs/design/code-mode-design.md §3/§4。

``run_repl(code, tools, timeout_s)`` spawn 执行器子进程，serve stdio JSON-RPC：
每个内层 tool 请求经 ``dispatch``（P0 此处接 evaluate_tool_call() 权限判定 +
真实工具；原型用确定性工具集）执行后回子进程；只有最终 return 值返回给调用方
（= 回 LLM 上下文的全部内容）。

``repl_tool_skeleton`` 展示 P0 注册为 Ginno StructuredTool 的形状（参数=code，
返回=return-only）。原型不接入生产图——接入是选项 A（P0 施工）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

_EXECUTOR = str(Path(__file__).with_name("executor.py"))


class ReplResult:
    def __init__(self, value, error, inner_calls, wall_s):
        self.value = value
        self.error = error
        self.inner_calls = inner_calls  # 内层工具调用次数（留在执行器，不进上下文）
        self.wall_s = wall_s

    def to_tool_output(self) -> str:
        """repl 工具回给 LLM 的内容 = 仅 return 值（return-only 上下文经济）。"""
        if self.error:
            return f"[code-mode error] {self.error}"
        return json.dumps(self.value, ensure_ascii=False)


def run_repl(
    code: str,
    tools: dict[str, Callable[[dict], object]],
    timeout_s: float = 60.0,
    permission: Callable[[str, dict], str] | None = None,
) -> ReplResult:
    """spawn 执行器 + serve RPC。permission(name,args)->'allow'|'deny'|'ask'。

    原型 permission 默认全 allow；P0 接 evaluate_tool_call()。ask 在原型里
    简化为 deny（设计 §7 P0：内层 ask 转外层一次确认，完整 interrupt 留 P1）。
    """
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, _EXECUTOR],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    inner = 0
    value = error = None
    try:
        proc.stdin.write(json.dumps({"code": code}) + "\n")
        proc.stdin.flush()
        deadline = time.perf_counter() + timeout_s
        while True:
            if time.perf_counter() > deadline:
                error = "timeout"
                break
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("method") == "return":
                p = msg.get("params", {})
                value, error = p.get("value"), p.get("error")
                break
            if msg.get("method") == "tool":
                inner += 1
                name = msg["params"]["name"]
                args = msg["params"].get("args", {})
                rid = msg["id"]
                try:
                    decision = permission(name, args) if permission else "allow"
                    if decision != "allow":
                        raise RuntimeError(f"permission {decision}: {name}")
                    if name not in tools:
                        raise RuntimeError(f"unknown tool: {name}")
                    result = tools[name](args)
                    resp = {"id": rid, "result": result}
                except Exception as e:  # noqa: BLE001
                    resp = {"id": rid, "error": f"{type(e).__name__}: {e}"}
                proc.stdin.write(json.dumps(resp) + "\n")
                proc.stdin.flush()
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            proc.kill()
    return ReplResult(value, error, inner, time.perf_counter() - t0)


def repl_tool_skeleton(code: str, tools: dict, timeout_s: float = 60.0) -> str:
    """P0 将注册为 StructuredTool 的形状：args={code}，返回 return-only 字符串。"""
    return run_repl(code, tools, timeout_s).to_tool_output()
