"""PyInstaller entry point.

uv run python -m ginno_runtime     # local dev
ginno exec -- <instruction>        # headless one-shot (benchmark harness)
pyinstaller --noconfirm --onedir src/ginno_runtime/__main__.py --name ginno-runtime
"""

import sys

# Native messaging host mode (browser-companion design §2 备用传输): Chrome
# spawns the frozen binary with --native-host; speak the length-prefixed JSON
# stdio protocol (port discovery + WS bridge) and exit when stdin closes.
if "--native-host" in sys.argv:
    from .browser import native_host as _nh

    _nh.run_host_entry()
    raise SystemExit(0)

# Headless exec mode (cli.py): `python -m ginno_runtime exec -- <instruction>`.
if len(sys.argv) > 1 and sys.argv[1] == "exec":
    from .cli import main as _cli_main

    _cli_main()
    raise SystemExit(0)

# Headless node mode (a2a/node.py): `python -m ginno_runtime node [--listen ...]`.
# 与 exec 同样必须在落到桌面 server 之前分流——否则 frozen/`-m` 形态下
# `node` 会被当成未知参数交给桌面 server，起的是 GUI 后端而不是 A2A node。
# console script `ginno node` 走 cli.py 的分流，两条入口都要通。
if len(sys.argv) > 1 and sys.argv[1] == "node":
    from .a2a.node import cli_main as _node_cli

    raise SystemExit(_node_cli(sys.argv[2:]))

from . import _frozen_imports  # noqa: F401  (kept for entry compatibility; now a no-op)
from .server import main

if __name__ == "__main__":
    main()
