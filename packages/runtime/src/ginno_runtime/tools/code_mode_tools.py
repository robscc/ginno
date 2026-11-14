"""Code Mode repl 工具——最小接入（原型验证，见 docs/design/code-mode-design.md）。

把独立原型 code_mode/host.run_repl 接进 Ginno 工具集：agent 写一段 Python
编排代码，repl 在子进程执行，内层 ``tool(name, args)`` 经 stdio RPC 回这里
分发到真实内置文件/shell 工具；只有最终 return 值回 agent 上下文（return-only）。

门控：env ``GINNO_CODE_MODE=1``（原型 opt-in；P0 迁 settings.code_mode.enabled）。
默认关闭——不影响生产工具集。

权限口径（原型，务必知悉）：内层调用经 host.run_repl 直接 invoke 真实工具，
**尚未逐个过 permission_node**（设计 §4 的 evaluate_tool_call 抽取属 P0）。
本接入仅用于 bypass_permissions 的隔离基准测试；生产启用前必须补权限复用。
"""

from __future__ import annotations

import os

from langchain_core.tools import tool


def code_mode_enabled() -> bool:
    return os.environ.get("GINNO_CODE_MODE", "") == "1"


def build_code_mode_tools(
    workspace: str | None = None,
    context_dirs: list[dict] | None = None,
    primary_path: str | None = None,
) -> list:
    if not code_mode_enabled():
        return []

    from ..code_mode import run_repl
    from .builtin import build_builtin_tools

    @tool
    def repl(code: str, timeout_s: int = 120) -> str:
        """Execute a Python orchestration script that calls multiple tools in
        ONE step, returning only the final value (Code Mode).

        Use this when a task needs several tool calls (search → read → edit →
        run → verify). Write Python; call tools via ``tool(name, args_dict)``;
        intermediate results stay in the script (NOT returned to you); end
        with ``return <small summary>``. Available inner tools: read_file,
        write_file, glob_files, grep_files, edit_file, bash — same args as the
        standalone tools. Example:

            hits = tool("grep_files", {"pattern": "retryPolicy", "path": "src"})
            target = hits.splitlines()[0].split(":")[0]
            ed = tool("edit_file", {"path": target, "old": "x: 3", "new": "x: 5"})
            out = tool("bash", {"command": "pytest -q"})
            return {"edited": target, "ok": "passed" in out}

        Prefer this over many separate tool calls for multi-step work: it cuts
        round-trips and keeps large intermediate output out of the context.
        """
        # 内层工具集：与外层同 workspace/挂载绑定的真实内置工具。session_id
        # 传 None——原型不重复快照 code-panel baseline。
        inner = build_builtin_tools(
            workspace, context_dirs=context_dirs, primary_path=primary_path
        )
        dispatch = {t.name: (lambda args, _t=t: _t.invoke(args)) for t in inner}
        res = run_repl(code, dispatch, timeout_s=float(timeout_s))
        return res.to_tool_output()

    return [repl]
