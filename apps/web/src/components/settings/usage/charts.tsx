"use client";

/** Shared chart primitives for Settings → 用量统计 (usage-stats-design.md §6).
 * Hand-rolled SVG + hand-rolled HTML on the app's tokens — same visual contract
 * as the prototype (docs/design/prototypes/usage-stats-prototype.html): stacked
 * bars with 2px gaps + rounded stack tops, recessive grid, hover tooltips. */

import { useCallback, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useTranslations } from "next-intl";

export const SERIES = {
  cache: "#059669", // 缓存读
  input: "#6366f1", // 输入（非缓存）
  output: "#8b5cf6", // 输出
} as const;

export const PROVIDER_COLORS: Record<string, string> = {
  anthropic: "#ea580c",
  openai: "#0284c7",
  custom: "#059669",
};
export function providerColor(p: string): string {
  return PROVIDER_COLORS[p] || "#6366f1";
}

export function fmt(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}K`;
  return String(Math.round(n));
}

/** Exact thousands-separated number — tooltips show billing-grade digits. */
export function exact(n: number | undefined | null): string {
  return Math.round(n || 0).toLocaleString("en-US");
}

export function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

/* ---- tooltip ---- */
export function useTip() {
  const [content, setContent] = useState<ReactNode>(null);
  const ref = useRef<HTMLDivElement | null>(null);

  const move = useCallback((ev: { clientX: number; clientY: number }) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    let x = ev.clientX + 14;
    let y = ev.clientY + 12;
    if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - 12;
    if (y + r.height > window.innerHeight - 8) y = ev.clientY - r.height - 10;
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
  }, []);

  const show = useCallback(
    (node: ReactNode, ev: { clientX: number; clientY: number }) => {
      setContent(node);
      // position after paint so the size is known
      requestAnimationFrame(() => move(ev));
    },
    [move],
  );
  const hide = useCallback(() => setContent(null), []);

  const tipEl = (
    <div
      ref={ref}
      role="tooltip"
      className="pointer-events-none fixed z-50 max-w-[320px] rounded-lg border border-line2 bg-card/95 px-3 py-2 text-[11.5px] leading-relaxed text-txt shadow-xl"
      style={{ display: content ? "block" : "none" }}
    >
      {content}
    </div>
  );
  return { show, hide, move, tipEl };
}

export function TipRow({ label, value, swatch }: { label: string; value: string; swatch?: string }) {
  return (
    <div className="flex justify-between gap-4">
      <span>
        {swatch && <i className="mr-1.5 inline-block h-2 w-2 rounded-[2px]" style={{ background: swatch }} />}
        {label}
      </span>
      <b className="tabular-nums text-txt">{value}</b>
    </div>
  );
}

/* ---- 2h cadence grid: sequential heat ramp + cell geometry ----
 *  Unlike the older SVG charts above (which hardcode dark hex), the cadence
 *  grid themes through CSS custom properties so it holds up in both themes.
 *  Pattern copied from globals.css: dark values on :root, light overrides on
 *  html.light.
 *  色阶不另起炉灶：空格子直接用 --card2，L1–L5 是 --card2 → --chart-1
 *  的 20/40/60/80/100% 混合（L5 恰为 --chart-1 本尊），跟全局 CVD 校验的
 *  图表色板同源。选中/hover 的强调色是 data 分组的主题青绿，与蓝阶
 *  色相分离，描边不会被当成最深档。 */
export const USAGE_GRID_CSS = `
:root {
  --ug-heat-0: rgb(var(--card2));
  --ug-heat-0-ring: rgb(var(--line));
  --ug-heat-1: #21314b;
  --ug-heat-2: #274672;
  --ug-heat-3: #2d5c98;
  --ug-heat-4: #3371bf;
  --ug-heat-5: #3987e5;
  --ug-accent: #2dd4bf;
}
html.light {
  --ug-heat-0: rgb(var(--card2));
  --ug-heat-0-ring: rgb(var(--line));
  --ug-heat-1: #cbdaf0;
  --ug-heat-2: #a3c2ea;
  --ug-heat-3: #7aa9e3;
  --ug-heat-4: #5291dd;
  --ug-heat-5: #2a78d6;
  --ug-accent: #0891b2;
}
.ug-scroll {
  padding: 34px 18px 14px; overflow-x: auto; max-height: 420px;
}
.ug-inner { display: flex; gap: 8px; min-width: min-content; }
.ug-gutter { display: grid; grid-template-rows: repeat(12, var(--ug-cell)); gap: var(--ug-gap); flex: none; }
.ug-gutter > span {
  font-size: 9.5px; color: rgb(var(--faint)); line-height: var(--ug-cell); height: var(--ug-cell);
  display: flex; align-items: center; font-variant-numeric: tabular-nums;
}
.ug-cols { display: flex; gap: var(--ug-gap); position: relative; min-width: min-content; }
.ug-day { position: relative; display: grid; grid-template-rows: repeat(12, var(--ug-cell)); gap: var(--ug-gap); flex: none; }
.ug-date {
  position: absolute; top: -19px; left: 1px; font-size: 9.5px; color: rgb(var(--faint));
  white-space: nowrap; font-variant-numeric: tabular-nums; pointer-events: none;
}
.ug-monthsep { position: absolute; top: -19px; bottom: -4px; width: 1px; background: rgb(var(--line2)); }
.ug-cell {
  width: var(--ug-cell); height: var(--ug-cell); border-radius: 2.5px; border: 0; padding: 0;
  background: var(--ug-heat-0); box-shadow: inset 0 0 0 1px var(--ug-heat-0-ring);
  cursor: pointer; transition: outline-color 0.09s ease;
}
.ug-cell.ug-l1 { background: var(--ug-heat-1); box-shadow: none; }
.ug-cell.ug-l2 { background: var(--ug-heat-2); box-shadow: none; }
.ug-cell.ug-l3 { background: var(--ug-heat-3); box-shadow: none; }
.ug-cell.ug-l4 { background: var(--ug-heat-4); box-shadow: none; }
.ug-cell.ug-l5 { background: var(--ug-heat-5); box-shadow: none; }
.ug-cell:hover { outline: 1.5px solid var(--ug-accent); outline-offset: 1px; }
.ug-cell:focus-visible { outline: 2px solid var(--ug-accent); outline-offset: 1px; }
.ug-cell[data-sel="1"] { outline: 2px solid var(--ug-accent); outline-offset: 1px; }
.ug-cell.ug-future {
  background: transparent; box-shadow: inset 0 0 0 1px rgb(var(--line));
  opacity: 0.5; cursor: default;
}
.ug-cell.ug-future:hover { outline: none; }
.ug-cell.ug-now { outline: 1.5px solid var(--ug-accent); outline-offset: 1px; }
.ug-swatch {
  display: inline-block; width: 11px; height: 11px; border-radius: 2.5px;
  box-shadow: inset 0 0 0 1px var(--ug-heat-0-ring);
}
.ug-swatch.ug-l1 { background: var(--ug-heat-1); box-shadow: none; }
.ug-swatch.ug-l2 { background: var(--ug-heat-2); box-shadow: none; }
.ug-swatch.ug-l3 { background: var(--ug-heat-3); box-shadow: none; }
.ug-swatch.ug-l4 { background: var(--ug-heat-4); box-shadow: none; }
.ug-swatch.ug-l5 { background: var(--ug-heat-5); box-shadow: none; }
@media (prefers-reduced-motion: reduce) {
  .ug-cell { transition: none; }
}
`;

/** Injects USAGE_GRID_CSS once per page; the grid reads every colour through
 * the custom properties above, so no component ever hardcodes a hex. */
export function UsageGridTokens() {
  return <style dangerouslySetInnerHTML={{ __html: USAGE_GRID_CSS }} />;
}

/** Level → class name (index 0 = empty/idle bin, which also covers days with
 * no telemetry file — there is no such distinction in the data contract). */
export const HEAT_CLASSES = ["", "ug-l1", "ug-l2", "ug-l3", "ug-l4", "ug-l5"] as const;

/** Raw CSS var reference for a ramp level (0 = empty/idle bin). Keeps the hex
 * inside this file — panels never spell a colour literal. */
export function heatVar(level: number): string {
  return level <= 0 ? "var(--ug-heat-0)" : `var(--ug-heat-${level})`;
}

/** Inline style for a legend swatch / accent-driven bit that needs the ramp
 * colour without a class. */
export function heatStyle(level: number): CSSProperties {
  return { background: heatVar(level) };
}
