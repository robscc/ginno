"""Chrome ``_locales`` 生成 — 单一来源（i18n-design.md §8）。

同时服务扩展目录的两个写入方：
  - ``packages/extension/build.py``（手动/dev 构建 → ~/.ginno/browser-extension）
  - runtime ``browser/native_host.py``（sidecar 启动时物化，dev checkout 与
    frozen PyInstaller bundle 都走这条）

放进 runtime 包内是为了 frozen bundle 自动打包本模块——与 content scripts
从 browser/scripts.py 生成是同一条教训（2026-10-02）：frozen 内不得依赖
仓库相对路径的源码文件。

输入是**已解析**的 web catalog 文档（{"ext": {...}} 域包裹结构），路径解析
由调用方负责（dev 找 apps/web/messages，frozen 找 spec datas 物化的
web_messages/，见 native_host._ext_messages_candidates）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# web catalog 目录名 → Chrome locale 目录名（Chrome 用下划线：zh_CN）
LOCALES = {"en": "en", "zh-CN": "zh_CN"}

# next-intl 简单占位符 {name}（ext 域不使用 ICU plural/select）
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def flatten(node: dict, prefix: str = "") -> dict:
    """{"ext": {"popup": {"connectedPort": "…"}}} → {"ext_popup_connectedPort": "…"}。

    Chrome message 名只允许 [a-zA-Z0-9_]，路径段原样用 "_" 连接。
    """
    out: dict = {}
    for key, value in node.items():
        flat = f"{prefix}_{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, flat))
        else:
            out[flat] = str(value)
    return out


def to_chrome_entry(text: str) -> dict:
    """{name} 占位符 → Chrome $NAME$ + placeholders（同名占位符只登记一次）。"""
    subs: list[tuple[str, int]] = []

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in [n for n, _ in subs]:
            subs.append((name, len(subs) + 1))
        return f"${name.upper()}$"

    entry: dict = {"message": PLACEHOLDER_RE.sub(repl, text)}
    if subs:
        entry["placeholders"] = {
            n.upper(): {"content": f"${i}"} for n, i in subs
        }
    return entry


def _chrome_messages(catalog: dict) -> dict:
    """校验顶层 ext 域并展平成 Chrome messages 名册。"""
    if not isinstance(catalog.get("ext"), dict):
        raise ValueError("missing top-level 'ext' domain key")
    return {key: to_chrome_entry(text) for key, text in flatten(catalog).items()}


def generate_locales(out: Path, catalogs: dict[str, dict]) -> None:
    """从已解析的 ext catalog 写 _locales/{en,zh_CN}/messages.json。

    ``catalogs``: web locale 目录名 → 解析后的完整 catalog JSON（含顶层
    "ext" 域键）。先全部校验再落盘：任一语言缺 ext 域都不写半个 _locales，
    避免出现「manifest 已改写 __MSG_* 而 messages 残缺」导致 Chrome 拒载。
    """
    messages_per_locale = {web: _chrome_messages(cat) for web, cat in catalogs.items()}
    for web_dir, messages in messages_per_locale.items():
        dst = out / "_locales" / LOCALES[web_dir]
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "messages.json").write_text(
            json.dumps(messages, ensure_ascii=False, indent=2) + "\n"
        )


def localize_manifest(out: Path) -> None:
    """输出目录的 manifest.json 改用 __MSG_* 引用（仅物化产物；src/ 不动）。"""
    mf_path = out / "manifest.json"
    mf = json.loads(mf_path.read_text())
    mf["default_locale"] = "en"
    mf["name"] = "__MSG_ext_manifest_name__"
    mf["description"] = "__MSG_ext_manifest_description__"
    mf["action"]["default_title"] = "__MSG_ext_manifest_defaultTitle__"
    mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=2) + "\n")


def generate_and_localize(out: Path, catalogs: dict[str, dict]) -> None:
    """generate_locales + localize_manifest，顺序保证：manifest 只在
    default locale 的 messages 落盘之后才改写 __MSG_*。"""
    generate_locales(out, catalogs)
    localize_manifest(out)


def load_catalogs(messages_root: Path) -> dict[str, dict] | None:
    """解析 messages_root 下 {en,zh-CN}/ext.json；任一文件缺失 → None
    （调用方尝试下一个候选根）。"""
    catalogs: dict[str, dict] = {}
    for web_dir in LOCALES:
        p = messages_root / web_dir / "ext.json"
        if not p.is_file():
            return None
        catalogs[web_dir] = json.loads(p.read_text(encoding="utf-8"))
    return catalogs
