"""PyInstaller entry script.

  pyinstaller --onefile --paths src --name ginno-runtime bin/ginno-runtime.py
"""
from ginno_runtime.server import main

if __name__ == "__main__":
    main()
