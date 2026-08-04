"""PyInstaller entry script.

  pyinstaller --noconfirm --onedir --paths src --name ginno-runtime bin/ginno-runtime.py
"""
import sys

# Hidden analysis mode: `ginno-runtime --analyze` execs a script piped on stdin
# and exits WITHOUT starting the HTTP server (never binds the sidecar port).
# tools/document_tools.py:analyze_table uses it in packaged builds, where
# sys.executable is this binary and `python -I -c` is unavailable. Checked
# before importing the heavy server stack so the subprocess stays light.
if "--analyze" in sys.argv:
    from ginno_runtime.analyze_runner import run as _analyze_run

    _analyze_run()
    raise SystemExit(0)

import ginno_runtime._frozen_imports  # noqa: F401  (kept for entry compatibility; now a no-op)
from ginno_runtime.server import main

if __name__ == "__main__":
    main()
