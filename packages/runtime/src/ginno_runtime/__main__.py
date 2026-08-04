"""PyInstaller entry point.

    uv run python -m ginno_runtime     # local dev
    pyinstaller --noconfirm --onedir src/ginno_runtime/__main__.py --name ginno-runtime
"""
import sys

# Hidden analysis mode (see bin/ginno-runtime.py): exec a piped-in script and
# exit without starting the HTTP server. Checked before the heavy imports.
if "--analyze" in sys.argv:
    from .analyze_runner import run as _analyze_run

    _analyze_run()
    raise SystemExit(0)

from . import _frozen_imports  # noqa: F401  (kept for entry compatibility; now a no-op)
from .server import main

if __name__ == "__main__":
    main()
