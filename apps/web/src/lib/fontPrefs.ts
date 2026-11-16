/**
 * 字体族 + 字号偏好（Settings → 通用 → 外观）。
 *
 * 存 localStorage 而不是 settings.json：纯前端渲染偏好，runtime 完全不需要
 * 知道它（与 `ginno-theme` / `ginno-mermaid` 同一取舍）。字号三档（小/中/大）
 * 通过根元素 font-size 缩放——Tailwind 的 text-* 与 spacing 多为 rem，根字号
 * 变化即整体等比缩放，medium=16px 与浏览器默认一致（不改变既有观感）。
 *
 * 字体族通过 CSS 变量 --app-font-family 注入，globals.css 的 body 引用它。
 * 读取同步（首帧即应用），写入后立即 apply，无需重进页面。
 */

export type FontFamily = "inter" | "system" | "serif" | "mono";
export type FontSize = "small" | "medium" | "large";

export interface FontPrefs {
  family: FontFamily;
  size: FontSize;
}

/** 各字体族的完整 stack（CJK 回退保证中文不缺字）。 */
export const FONT_FAMILY_STACKS: Record<FontFamily, string> = {
  inter: 'Inter, ui-sans-serif, system-ui, -apple-system, "PingFang SC", sans-serif',
  system:
    'ui-sans-serif, system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
  serif: 'ui-serif, Georgia, "Songti SC", "SimSun", serif',
  mono: 'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
};

/** 三档根字号（px）。medium=16 即浏览器默认，保持既有观感不变。 */
export const FONT_SIZE_PX: Record<FontSize, number> = {
  small: 14,
  medium: 16,
  large: 18,
};

export const FONT_DEFAULT: FontPrefs = { family: "inter", size: "medium" };

export const FONT_FAMILY_ORDER: FontFamily[] = ["inter", "system", "serif", "mono"];
export const FONT_SIZE_ORDER: FontSize[] = ["small", "medium", "large"];

const KEY = "ginno-font-prefs";

/** 纯函数：校验任意输入 → 合法偏好（未知/ malformed 字段回退默认）。 */
export function parseFontPrefs(raw: unknown): FontPrefs {
  const d = { ...FONT_DEFAULT };
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (typeof o.family === "string" && o.family in FONT_FAMILY_STACKS) {
      d.family = o.family as FontFamily;
    }
    if (typeof o.size === "string" && o.size in FONT_SIZE_PX) {
      d.size = o.size as FontSize;
    }
  }
  return d;
}

/** 同步读取。localStorage 不可用（隐私模式等）或 JSON 损坏时返回默认。 */
export function readFontPrefs(): FontPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    return parseFontPrefs(raw ? JSON.parse(raw) : null);
  } catch {
    return { ...FONT_DEFAULT };
  }
}

/** 应用到根元素：字号 + 字体族变量。SSR / 无 document 时 no-op。 */
export function applyFontPrefs(p: FontPrefs): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.style.fontSize = `${FONT_SIZE_PX[p.size]}px`;
  root.style.setProperty("--app-font-family", FONT_FAMILY_STACKS[p.family]);
}

/** 合并写入 localStorage 并立即应用，返回合并后的偏好。 */
export function writeFontPrefs(next: Partial<FontPrefs>): FontPrefs {
  const merged = parseFontPrefs({ ...readFontPrefs(), ...next });
  try {
    localStorage.setItem(KEY, JSON.stringify(merged));
  } catch {
    /* 只是偏好，丢了不影响功能 */
  }
  applyFontPrefs(merged);
  return merged;
}