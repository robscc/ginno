#!/usr/bin/env python3
"""Build the Ginno browser extension into a loadable folder.

Content scripts (accessibility-tree / page-bridge) are generated from the
SAME source the B 轨 injects (runtime browser/scripts.py) — one source of
truth, both tracks identical. The native messaging host script is copied in
too. Output: <out>/ (default: ~/.ginno/browser-extension, the folder the user
loads via chrome://extensions — the materialization flow in
browser-companion-extension-design.md §7.3).

i18n (i18n-design.md §8): 用户可见文案单一来源是 apps/web/messages/{en,zh-CN}/
ext.json（"ext" 域）。展平成 Chrome 标准 _locales/{en,zh_CN}/messages.json
（message + placeholders；Chrome 按浏览器语言自动选边）+ 输出 manifest 的
__MSG_* 改写的**生成逻辑本体**在 runtime 的 browser/ext_locales.py（单一来源，
与 native_host.py 物化路径共用本模块）；本脚本只负责读取 catalog 并调用。
src/ 里的 manifest 保持英文明文，因为 runtime 物化的固定文件清单历史上不含
_locales（2026-10-04 起 native_host 物化同样生成 _locales + 改写 manifest），
扩展代码侧仍保留内嵌 en 兜底（见 popup.js / visual-indicator.js / background.js）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
ROOT = HERE.parent.parent
RUNTIME_SCRIPTS = HERE.parent / "runtime" / "src" / "ginno_runtime" / "browser" / "scripts.py"
EXT_LOCALES = HERE.parent / "runtime" / "src" / "ginno_runtime" / "browser" / "ext_locales.py"
WEB_MESSAGES = ROOT / "apps" / "web" / "messages"

EXTENSION_ID = "jlmheiiglpdikeihgjefjhmoakfllhkm"


def load_scripts_module():
    ns: dict = {}
    exec(compile(RUNTIME_SCRIPTS.read_text(), str(RUNTIME_SCRIPTS), "exec"), ns)
    return ns


def load_ext_locales_module() -> dict:
    ns: dict = {}
    exec(compile(EXT_LOCALES.read_text(), str(EXT_LOCALES), "exec"), ns)
    return ns


def generate_locales(out: Path) -> None:
    """从 apps/web/messages 的 ext.json 生成 _locales + 改写 manifest
    （实际逻辑在 runtime 的 ext_locales 模块，见模块 docstring）。"""
    extl = load_ext_locales_module()
    catalogs = {}
    for web_dir in extl["LOCALES"]:
        src = WEB_MESSAGES / web_dir / "ext.json"
        catalogs[web_dir] = json.loads(src.read_text())
    try:
        extl["generate_and_localize"](out, catalogs)
    except ValueError as exc:
        raise SystemExit(f"{WEB_MESSAGES}: {exc}") from exc


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
    # i18n: _locales 生成 + 输出 manifest 的 __MSG_* 改写（失败即构建失败——
    # manifest 引用了 __MSG_* 而 _locales 缺失会让 Chrome 拒载扩展）
    generate_locales(out)
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
