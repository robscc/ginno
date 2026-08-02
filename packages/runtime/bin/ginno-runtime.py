"""PyInstaller entry script.

  pyinstaller --onefile --paths src --name ginno-runtime bin/ginno-runtime.py
"""
import ginno_runtime._frozen_imports  # noqa: F401  (force-bundle docs extras)
from ginno_runtime.server import main

if __name__ == "__main__":
    main()
