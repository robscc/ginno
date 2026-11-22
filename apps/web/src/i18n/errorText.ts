/**
 * 后端直通错误串 → 本地化（中文化尾巴 ②）。
 *
 * 后端 ``error`` 字段是稳定英文契约（API 单测钉住，如
 * ``{"ok": false, "error": "knowledge not configured"}``），kb 页 /
 * Web 搜索设置等处曾把它原样直显。本模块按「前端 catalog 映射」范式
 * （同 uiText.ts 的非 hook 读取器架构）：静态 import 双语言 catalog +
 * locale 镜像，i18n provider 在 locale 变化时调 setErrorTextLocale()
 * 同步；KNOWN 前缀表把已知错误串映射到 core.json 的 errors.* 键，
 * 未知 / 动态串原样返回——后端零改动，行为向后兼容。
 *
 * 只收用户可见且高频的串（knowledge 类 + 搜索引擎配置类）；404 类
 * （run/workflow/session/task not found）与开发者语义串不收，列出
 * 不动（见 PR 描述）。
 */

import coreEn from "../../messages/en/core.json";
import coreZh from "../../messages/zh-CN/core.json";
import { defaultLocale, type Locale } from "./config";

type ErrTree = Record<string, string>;

const CATALOGS: Record<Locale, ErrTree> = {
  en: coreEn.errors as ErrTree,
  "zh-CN": coreZh.errors as ErrTree,
};

/** en catalog 的 errors.* 键并集——拼错 key 编译失败（同 uiText 的点路径收敛）。 */
type ErrKey = keyof (typeof coreEn)["errors"];

/** 已知错误串前缀（小写）→ catalog 键。更长前缀排前面：`knowledge
 * vault not configured` 必须先于 `knowledge not configured` 匹配。
 * 括号里的动态尾巴（设置路径等）不参与匹配，本地化文案自行表述。 */
const KNOWN: Array<[prefix: string, key: ErrKey]> = [
  ["knowledge vault not configured", "knowledgeVaultNotConfigured"],
  ["knowledge not configured", "knowledgeNotConfigured"],
  ["searxng base_url not configured", "searxngNotConfigured"],
  ["tavily api_key not configured", "tavilyNotConfigured"],
];

let _locale: Locale = defaultLocale;

/** provider 在 locale 变化时同步（含首帧渲染期赋值，保证非 hook 调用即时生效）。 */
export function setErrorTextLocale(locale: Locale): void {
  _locale = locale;
}

/** 已知后端错误串 → 当前 locale 文案；未知 / 空串 / 动态串原样返回
 * （空输入返回空串，调用方 ``apiErrorText(e) || t("fallback")`` 的
 * 兜底语义不变）。纯函数，node 环境可单测。 */
export function apiErrorText(err: string | null | undefined): string {
  const text = (err || "").trim();
  if (!text) return "";
  const lower = text.toLowerCase();
  for (const [prefix, key] of KNOWN) {
    if (lower.startsWith(prefix)) {
      return CATALOGS[_locale][key] ?? text;
    }
  }
  return text;
}
