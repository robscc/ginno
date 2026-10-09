"""PyInstaller entry script.

  pyinstaller --noconfirm --onedir --paths src --name ginno-runtime bin/ginno-runtime.py
"""
import sys

import ginno_runtime._frozen_imports  # noqa: F401  (kept for entry compatibility; now a no-op)
from ginno_runtime.server import main

if __name__ == "__main__":
    main()
