"""Code Mode 原型包（选项 B 原型，非生产接入）。见 docs/design/code-mode-design.md。"""

from .host import ReplResult, repl_tool_skeleton, run_repl

__all__ = ["run_repl", "repl_tool_skeleton", "ReplResult"]
