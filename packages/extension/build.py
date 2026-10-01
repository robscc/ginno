#!/usr/bin/env python3
"""Build the Ginno browser extension into a loadable folder.

Content scripts (accessibility-tree / page-bridge) are generated from the
SAME source the B 轨 injects (runtime browser/scripts.py) — one source of
truth, both tracks identical. The native messaging host script is copied in
too. Output: <out>/ (default: ~/.ginno/browser-extension, the folder the user
loads via chrome://extensions — the materialization flow in
browser-companion-extension-design.md §7.3).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
RUNTIME_SCRIPTS = HERE.parent / "runtime" / "src" / "ginno_runtime" / "browser" / "scripts.py"

EXTENSION_ID = "jlmheiiglpdikeihgjefjhmoakfllhkm"


def load_scripts_module():
    ns: dict = {}
    exec(compile(RUNTIME_SCRIPTS.read_text(), str(RUNTIME_SCRIPTS), "exec"), ns)
    return ns


def build(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    ns = load_scripts_module()
    cs = out / "content-scripts"
    cs.mkdir(exist_ok=True)
    (cs / "accessibility-tree.js").write_text(ns["ACCESSIBILITY_TREE_JS"])
    (cs / "page-bridge.js").write_text(ns["PAGE_BRIDGE_JS"])
    for name in ("background.js", "popup.html", "popup.js", "manifest.json"):
        shutil.copy2(SRC / name, out / name)
    vi = (cs / "visual-indicator.js").exists()
    shutil.copy2(SRC / "content-scripts" / "visual-indicator.js",
                 cs / "visual-indicator.js")
    # native messaging host manifest template (installed by the sidecar with
    # the right binary path; see runtime browser/native_host.py)
    host_src = HERE / "native-host" / "ginno_browser_host.py"
    if host_src.exists():
        (out / "ginno_browser_host.py").write_text(host_src.read_text())
    version = json.loads((SRC / "manifest.json").read_text())["version"]
    (out / "VERSION.json").write_text(json.dumps(
        {"version": version, "extensionId": EXTENSION_ID,
         "builtFrom": "packages/extension"}, ensure_ascii=False, indent=2))
    print(f"built extension v{version} → {out}")
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    if target is None:
        import os
        target = Path(os.path.expanduser("~/.ginno/browser-extension"))
    build(target)
