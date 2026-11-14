"""Code Mode 执行器（子进程侧）——原型，见 docs/design/code-mode-design.md §3。

跑 LLM 写的编排代码；代码里每个 ``tool(name, args)`` 经 stdio JSON-RPC
回宿主（host.py）走权限+执行，结果留在本子进程，只有最终 return 值回宿主。

协议（行分隔 JSON）：
  子→父  {"id":N,"method":"tool","params":{"name":..,"args":{..}}}
  父→子  {"id":N,"result":..}  或  {"id":N,"error":".."}
  子→父  {"id":0,"method":"return","params":{"value":..}}

启动：宿主把代码作为 stdin 第一行（JSON {"code": "..."}）写入；本进程读取后
执行，期间 tool() 同步往返。执行器自身不持有任何工具能力——只有 RPC client。
"""

from __future__ import annotations

import json
import sys

_next_id = 0


def _rpc(method: str, params: dict):
    global _next_id
    _next_id += 1
    rid = _next_id
    sys.stdout.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
    sys.stdout.flush()
    # 同步等宿主回应（原型单线程；P1 并行原语再改异步）
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("host closed the RPC channel")
    msg = json.loads(line)
    if "error" in msg:
        raise RuntimeError(msg["error"])
    return msg.get("result")


def tool(name: str, args: dict | None = None):
    """代码内唯一工具入口：RPC 回宿主，走与正常 tool_call 相同的权限+执行。"""
    return _rpc("tool", {"name": name, "args": args or {}})


def log(*parts):
    """显式回传少量摘要到宿主日志（不进 LLM 上下文，原型仅 stderr）。"""
    sys.stderr.write(" ".join(str(p) for p in parts) + "\n")


def _run(code: str):
    # 把代码包成函数体，允许 `return`；注入 tool/log。
    ns = {"tool": tool, "log": log}
    indented = "\n".join("    " + ln for ln in code.splitlines())
    wrapper = "def __code_main__():\n" + (indented or "    pass") + "\n"
    exec(compile(wrapper, "<code-mode>", "exec"), ns)  # noqa: S102 — 原型即任意代码执行
    return ns["__code_main__"]()


def main() -> None:
    first = sys.stdin.readline()
    if not first:
        return
    code = json.loads(first).get("code", "")
    try:
        value = _run(code)
        err = None
    except Exception as e:  # noqa: BLE001
        value, err = None, f"{type(e).__name__}: {e}"
    payload = {"value": value}
    if err:
        payload["error"] = err
    sys.stdout.write(json.dumps({"id": 0, "method": "return", "params": payload}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
