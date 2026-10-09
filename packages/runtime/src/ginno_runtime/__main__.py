"""PyInstaller entry point.

    uv run python -m ginno_runtime     # local dev
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

from . import _frozen_imports  # noqa: F401  (kept for entry compatibility; now a no-op)
from .server import main

if __name__ == "__main__":
    main()
