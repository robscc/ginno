/* 界面明暗主题的「初始值解析」（quick-fix D6）。
 *
 * 历史行为：无本地选择时硬编码 dark。新行为（本文件是唯一事实源，
 * AppShell 挂载与 GeneralSettings 初始化共用）：
 *   1. localStorage 里已有手动选择（ginno-theme = light|dark）→ 尊重本地；
 *   2. 无本地选择 → 跟随 prefers-color-scheme（浅色系统用 light）；
 *      且**不落盘**——系统以后切深浅色，下次启动仍跟着走；只有用户手动
 *      切换（applyTheme 默认路径）才写入本地选择并固定下来；
 *   3. matchMedia 不可用（SSR / 异常环境）→ 维持旧默认 dark。
 *
 * 纯函数 + 显式传依赖（stored / prefersLight），node 环境无 DOM 直测；
 * matchMedia 用最小 fake（{matches: boolean}）模拟。 */

export const THEME_KEY = "ginno-theme";

export type Theme = "light" | "dark";

/** localStorage 读取（吞异常，与既有 ginno-theme 读法一致）。 */
export function readStoredTheme(): string | null {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

/** 读 prefers-color-scheme。返回 null 表示环境里没有 matchMedia
 *  （SSR / 老 WebView），调用方应退回 legacy 默认而不是瞎猜。 */
export function systemPrefersLight(
  mm: { matches: boolean } | null | undefined,
): boolean | null {
  if (!mm || typeof mm.matches !== "boolean") return null;
  return mm.matches;
}

/** 浏览器侧便捷封装：window.matchMedia 不可用时返回 null。 */
export function systemPrefersLightFromWindow(): boolean | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return null;
  }
  try {
    return systemPrefersLight(window.matchMedia("(prefers-color-scheme: light)"));
  } catch {
    return null;
  }
}

/** 初始主题裁决：本地手动选择 > 系统偏好 > dark（legacy 默认）。
 *  stored 只认 "light"/"dark" 两个显式值，脏值视为无选择。 */
export function resolveInitialTheme(
  stored: string | null | undefined,
  prefersLight: boolean | null,
): Theme {
  if (stored === "light" || stored === "dark") return stored;
  if (prefersLight === null) return "dark";
  return prefersLight ? "light" : "dark";
}

/** AppShell / GeneralSettings 共用的启动解析：一步拿到初始主题。 */
export function initialTheme(): Theme {
  return resolveInitialTheme(readStoredTheme(), systemPrefersLightFromWindow());
}
