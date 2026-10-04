/**
 * composer 域的非 hook 翻译辅助。
 *
 * commandMenu.ts（纯逻辑模块）与 useSummarizeFlow.ts（文案多在异步回调里
 * 成文，无渲染期响应式需求）不适合走 useTranslations 的 hook 路径。这里复用
 * provider.tsx 既有导出 currentLocale()（模块级 _currentLocale 镜像，渲染期
 * 同步赋值，从首帧起即为正确值），直接静态 import 两份 composer catalog 选边
 * ——与 provider 的 deep-merge 各走各的，不互相依赖（i18n-design.md §4.1）。
 * 仅本域使用，不动其他域。
 */
import type { Locale } from "@/i18n/config";
import { currentLocale } from "@/i18n/provider";
import en from "../../../messages/en/composer.json";
import zhCn from "../../../messages/zh-CN/composer.json";

/** en 为 source 语言，zh-CN 结构必须与 en 成对（make check 的 catalog diff 兜底）。 */
export type ComposerCatalog = typeof en;

const CATALOGS: Record<Locale, ComposerCatalog> = { en, "zh-CN": zhCn };

/** 当前 locale 的 composer catalog（SSG/未初始化时 currentLocale 落 en）。 */
export function composerCatalog(): ComposerCatalog {
  return CATALOGS[currentLocale()];
}

/**
 * {name} 占位符插值（ICU 子集，与 next-intl 的 messages 写法保持同形，
 * 便于日后迁入 hook 路径）。缺参时占位符原样保留，便于发现调用方笔误。
 */
export function fmt(tpl: string, params?: Record<string, string | number>): string {
  if (!params) return tpl;
  return tpl.replace(/\{(\w+)\}/g, (raw, key: string) =>
    Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : raw,
  );
}
