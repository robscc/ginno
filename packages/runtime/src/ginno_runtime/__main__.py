"""PyInstaller entry point.

    uv run python -m ginno_runtime     # local dev
    pyinstaller --onefile src/ginno_runtime/__main__.py --name ginno-runtime
"""

from . import _frozen_imports  # noqa: F401  (force-bundle docs extras in frozen builds)
from .server import main

if __name__ == "__main__":
    main()
