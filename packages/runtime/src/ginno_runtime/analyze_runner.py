"""Stdin-script runner used as the isolated interpreter for ``analyze_table``.

In packaged (PyInstaller) builds ``sys.executable`` is the ``ginno-runtime``
binary itself, not a Python interpreter, so ``document_tools._run_analysis``
cannot spawn ``python -I -c <script>``. Instead it launches
``ginno-runtime --analyze`` and pipes the fully-rendered analysis script on
stdin; this entry point executes it in a fresh namespace and lets the script
print its single JSON result line to stdout.

This module is imported *before* the heavy server stack (see
``bin/ginno-runtime.py``) so the analysis subprocess pays only the pandas
import cost — not the whole langchain/langgraph graph — and, crucially, never
starts the HTTP server or binds the sidecar port (which is what a naive
``[sys.executable, "-c", ...]`` would do in a frozen build: spin up a second
server that collides with the running sidecar).
"""

from __future__ import annotations

import sys


def run() -> None:
    script = sys.stdin.read()
    # Fresh namespace; the script is self-contained and prints one JSON line.
    # Exceptions propagate to stderr exactly as they would under `python -c`,
    # so the caller's "no JSON result" handling behaves identically.
    exec(compile(script, "<analyze_table>", "exec"), {"__name__": "__analyze__"})


if __name__ == "__main__":
    run()
