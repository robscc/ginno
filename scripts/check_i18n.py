#!/usr/bin/env python3
"""i18n catalog 一致性检查（i18n-design.md §10.2，`make check` 前置检查）。

覆盖两组 catalog：
  1. web:     apps/web/messages/{en,zh-CN}/*.json（域包裹结构，en 为 source）
  2. runtime: packages/runtime/src/ginno_runtime/i18n/{en.json,zh_CN.json}（扁平点号 key）

检查项：
  ① JSON 可解析（哪个文件坏了一目了然）
  ② key 集合双向 diff（递归展开域包裹后全域对比；缺/多都报错）
  ③ ICU 占位符一致性（同名同数量；解析简化集 {name} 与 {count, plural, ...}——
     简化集指取占位符首段名字，不校验复数规则内部结构）

任何一项失败：输出明细并以非零码退出。发现不一致时修 catalog，不要放宽本检查。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_MESSAGES = ROOT / "apps" / "web" / "messages"
RUNTIME_I18N = ROOT / "packages" / "runtime" / "src" / "ginno_runtime" / "i18n"

# 组名 → locale → 源（目录 = 多域 json 文件；单文件 = 扁平 catalog）
GROUPS: dict[str, dict[str, Path]] = {
    "web": {"en": WEB_MESSAGES / "en", "zh-CN": WEB_MESSAGES / "zh-CN"},
    "runtime": {"en": RUNTIME_I18N / "en.json", "zh-CN": RUNTIME_I18N / "zh_CN.json"},
}


def flatten(node: object, prefix: str = "") -> dict[str, str]:
    """递归展开 {"ext": {"popup": {"title": "…"}}} → {"ext.popup.title": "…"}。

    runtime catalog 本就是扁平点号 key，经此函数是恒等变换。叶子若不是
    字符串（误放数字/嵌套结构到叶子层）按 str() 收进 key 集，占位符解析
    对其返回空集——类型问题由 ①的 JSON 检查与 code review 兜住。
    """
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            full = f"{prefix}.{key}" if prefix else key
            out.update(flatten(value, full))
    else:
        out[prefix] = node if isinstance(node, str) else str(node)
    return out


def placeholder_names(s: str) -> set[str]:
    """提取 ICU 简化占位符名集合：{name} / {count, plural, ...} → 首段名字。

    按花括号配对扫描（嵌套 plural 子块计入深度），只取顶层 token，且仅当
    首段是合法 ICU 参数名（[A-Za-z_][A-Za-z0-9_]*）才算占位符——文案里
    包裹的 JSON 示例（如 '{"when":"<expression>"}'，ICU 单引号转义的字面
    量）不是占位符，不计入对比。
    """
    names: set[str] = set()
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                token = s[start + 1 : i]
                name = token.split(",", 1)[0].strip()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    names.add(name)
    return names


def load_group(source: Path) -> dict[str, str]:
    """加载一个 locale 的 catalog：目录（逐个 *.json 合并）或单 json 文件。"""
    catalog: dict[str, str] = {}
    files = sorted(source.glob("*.json")) if source.is_dir() else [source]
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        catalog.update(flatten(data))
    return catalog


def main() -> int:
    errors: list[str] = []
    # locale → 合并全域后的 key→文案（web + runtime 装进同一命名空间对比）
    catalogs: dict[str, dict[str, str]] = {"en": {}, "zh-CN": {}}
    counts: dict[str, dict[str, int]] = {g: {} for g in GROUPS}

    # ① JSON 可解析 + 加载
    for group, locales in GROUPS.items():
        for locale, source in locales.items():
            try:
                catalog = load_group(source)
            except (OSError, ValueError) as exc:
                errors.append(f"[json] {source}: {exc}")
                catalog = {}
            catalogs[locale].update(catalog)
            counts[group][locale] = len(catalog)
    if errors:
        print("\n".join(errors))
        print("aborted: catalog 加载失败，先修 JSON")
        return 1
    en, zh = catalogs["en"], catalogs["zh-CN"]

    # ② key 集合双向 diff（缺/多都报）
    en_keys, zh_keys = set(en), set(zh)
    for key in sorted(en_keys - zh_keys):
        errors.append(f"[missing-in-zh-CN] {key} = {en[key]!r}")
    for key in sorted(zh_keys - en_keys):
        errors.append(f"[extra-in-zh-CN] {key} = {zh[key]!r}")

    # ③ ICU 占位符一致性（仅双方都存在的 key）
    for key in sorted(en_keys & zh_keys):
        en_ph, zh_ph = placeholder_names(en[key]), placeholder_names(zh[key])
        if en_ph != zh_ph:
            errors.append(
                f"[placeholders] {key}: en={sorted(en_ph)} vs zh-CN={sorted(zh_ph)}"
            )

    total = len(en_keys | zh_keys)
    if errors:
        print(f"❌ check_i18n: {len(errors)} 处不一致（扫描 key 总数 {total}）\n")
        print("\n".join(errors))
        return 1
    detail = " + ".join(
        f"{group} {counts[group]['en']}" for group in GROUPS
    )
    print(f"✅ check_i18n: en/zh-CN catalog 一致（{detail}，共 {total} key），占位符一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
