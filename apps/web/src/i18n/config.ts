/**
 * i18n 基础配置（无 i18n 路由模式）。
 *
 * Ginno 是静态导出 + Tauri webview，语言由 settings 决定而非 URL，
 * 因此不走 locale 路由 / middleware。settings.language 存 "auto" | "en" | "zh-CN"，
 * "auto" 仅在前端按 navigator.language 解析（zh 开头 → zh-CN，否则 en）；
 * 不支持的系统语言（如 ja）静默落 en。
 */

export const locales = ["en", "zh-CN"] as const;
export type Locale = (typeof locales)[number];

/** source 语言 / catalog 权威；en 缺 key 时 next-intl 兜底回落 key 原样。 */
export const defaultLocale: Locale = "en";

/** settings.language 的原始取值（"auto" 待前端解析）。 */
export type LanguageSetting = "auto" | Locale;

/** localStorage 镜像 key：首屏同步读取，避免 settings 往返导致的首帧闪动。 */
export const LANGUAGE_LS_KEY = "ginno.language";

/** "auto" 解析：系统语言 zh 开头 → zh-CN，否则 en。 */
export function resolveAuto(tag?: string | null): Locale {
  return !!tag && tag.toLowerCase().startsWith("zh") ? "zh-CN" : defaultLocale;
}

/** 宽限 settings 里读到的 language 值（缺省/非法值一律按 "auto"）。 */
export function normalizeLanguageSetting(v: unknown): LanguageSetting {
  return v === "en" || v === "zh-CN" || v === "auto" ? v : "auto";
}
