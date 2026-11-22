/* Quick-fix D7 回归：code 面板三处硬编码暗色档颜色改走双主题 CSS 变量。
 * 本测试做三件事：
 *  1. 三个组件源码不再残留旧硬编码（#f59e0b/#06b6d4/#4caf7d/#d19a66/
 *     #4a9eff/rgba(74,158,255)），改用 --git-* 、--tab-* 、--diff-accent 系列变量；
 *  2. globals.css 的 :root 与 html.light 各自定义全套变量（漏一个 = 该处
 *     无色，静默退化）；
 *  3. 用 WCAG 相对亮度公式实算对比度：亮档对白底 ≥4.5:1（AA），暗档对
 *     暗底（--base #0a0a0f）≥4.5:1——这是当初改动的动机量化（原值白底
 *     仅 2.2~2.8:1）。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const treeUtils = read("./treeUtils.ts");
const editorTabs = read("./EditorTabs.tsx");
const diffOverlay = read("./InlineDiffOverlay.tsx");
const css = read("../../../app/globals.css");

const VARS = [
  "--git-mod",
  "--git-copy",
  "--tab-add",
  "--tab-mod",
  "--diff-accent",
  "--diff-accent-rgb",
] as const;

describe("code 面板三组件改用主题变量（quick-fix D7）", () => {
  it("treeUtils：M/C 徽标走 --git-mod/--git-copy，无残留硬编码 hex", () => {
    expect(treeUtils).toContain('"text-[var(--git-mod)]"');
    expect(treeUtils).toContain('"text-[var(--git-copy)]"');
    expect(treeUtils).not.toContain("#f59e0b");
    expect(treeUtils).not.toContain("#06b6d4");
  });

  it("EditorTabs：A/M 标记走 --tab-add/--tab-mod，无残留硬编码 hex", () => {
    expect(editorTabs).toContain('"text-[var(--tab-add)]"');
    expect(editorTabs).toContain('"text-[var(--tab-mod)]"');
    expect(editorTabs).not.toContain("#4caf7d");
    expect(editorTabs).not.toContain("#d19a66");
  });

  it("InlineDiffOverlay：全部强调色走 --diff-accent(_rgb)，无残留 #4a9eff/rgba", () => {
    expect(diffOverlay).not.toContain("#4a9eff");
    expect(diffOverlay).not.toContain("74,158,255");
    expect(diffOverlay.match(/text-\[var\(--diff-accent\)\]/g)?.length).toBe(4);
    expect(diffOverlay).toContain("border-[rgb(var(--diff-accent-rgb)_/_0.3)]");
    expect(diffOverlay).toContain("bg-[rgb(var(--diff-accent-rgb)_/_0.1)]");
    expect(diffOverlay.match(/_\/_0\.15\)\]/g)?.length).toBe(3);
  });

  it("globals.css：:root 与 html.light 都定义全套变量", () => {
    const lightAt = css.indexOf("html.light {");
    const root = css.slice(0, lightAt);
    const light = css.slice(lightAt);
    for (const v of VARS) {
      expect(root, `:root 缺 ${v}`).toMatch(new RegExp(`${v}:\\s*[^;]+;`));
      expect(light, `html.light 缺 ${v}`).toMatch(new RegExp(`${v}:\\s*[^;]+;`));
    }
  });
});

// ---- WCAG 对比度实算（动机量化 + 亮暗双档门槛）---- //

function luminance(hex: string): number {
  const n = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [la, lb] = [luminance(a), luminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function varOf(block: string, name: string): string {
  const m = block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!m) throw new Error(`${name} 未找到 hex 定义`);
  return m[1];
}

describe("双主题档位对比度门槛（WCAG）", () => {
  const lightAt = css.indexOf("html.light {");
  const root = css.slice(0, lightAt);
  const light = css.slice(lightAt);
  const DARK_BG = "#0a0a0f"; // :root --base 10 10 15
  const LIGHT_BG = "#ffffff"; // html.light --panel/--card

  const COLOR_VARS = ["--git-mod", "--git-copy", "--tab-add", "--tab-mod", "--diff-accent"];

  it.each(COLOR_VARS)("亮档 %s 对白底 ≥ 4.5:1", (v) => {
    expect(contrast(varOf(light, v), LIGHT_BG)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(COLOR_VARS)("暗档 %s 对暗底 ≥ 4.5:1（保持原视觉）", (v) => {
    expect(contrast(varOf(root, v), DARK_BG)).toBeGreaterThanOrEqual(4.5);
  });

  it("旧硬编码值确实不达标（动机钉住，防止改回去）", () => {
    for (const old of ["#f59e0b", "#06b6d4", "#4caf7d", "#d19a66", "#4a9eff"]) {
      expect(contrast(old, LIGHT_BG)).toBeLessThan(4.5);
    }
  });
});
