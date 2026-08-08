"use client";

/** Shared chart primitives for Settings → 用量统计 (usage-stats-design.md §6).
 * Hand-rolled SVG on the app's dark tokens — same visual contract as the
 * prototype (docs/design/prototypes/usage-stats-prototype.html): stacked bars
 * with 2px gaps + rounded stack tops, recessive grid, hover tooltips. */

import { useCallback, useRef, useState, type ReactNode } from "react";

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

export function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

export function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (m * p >= v) return m * p;
  }
  return 10 * p;
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
      className="pointer-events-none fixed z-50 max-w-[260px] rounded-lg border border-line2 bg-[#101018]/95 px-3 py-2 text-[11.5px] leading-relaxed text-muted shadow-xl"
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

/* ---- stacked-bar geometry ---- */
interface Part {
  v: number;
  color: string;
}

function roundTopRect(x: number, y: number, w: number, h: number, r: number): string {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}

const GAP = 2;
const RAD = 4;

/** One stacked column (parts bottom→top), 2px gaps, rounded stack top. */
function stackColumn(x: number, yBase: number, w: number, parts: Part[], scale: number): ReactNode[] {
  const visible = parts.filter((p) => p.v > 0);
  if (!visible.length) return [];
  const out: ReactNode[] = [];
  let y = yBase;
  visible.forEach((p, idx) => {
    const ph = Math.max(1.2, p.v * scale);
    const isTop = idx === visible.length - 1;
    const h = isTop ? ph : Math.max(0.8, ph - GAP);
    y -= ph;
    out.push(
      isTop ? (
        <path key={idx} d={roundTopRect(x, y, w, h, RAD)} fill={p.color} />
      ) : (
        <rect key={idx} x={x} y={y} width={w} height={h} fill={p.color} />
      ),
    );
  });
  return out;
}

export interface StackDatum {
  key: string;
  label: string; // x-axis label
  cache: number;
  inputNet: number;
  output: number;
}

/** Generic stacked bar chart. Hover any bar for the tooltip from `tipFor`;
 * click fires `onPick(key)`. `selected` gets a teal marker + full opacity. */
export function StackedBars({
  data,
  height = 250,
  selected,
  onPick,
  tipFor,
  xEvery,
  ariaLabel,
}: {
  data: StackDatum[];
  height?: number;
  selected?: string;
  onPick?: (key: string) => void;
  tipFor: (d: StackDatum, i: number) => ReactNode;
  xEvery?: number;
  ariaLabel: string;
}) {
  const { show, hide, move, tipEl } = useTip();
  const W = 960;
  const L = 48;
  const R = 10;
  const T = 12;
  const B = 26;
  const iw = W - L - R;
  const ih = height - T - B;
  const n = data.length;
  const max = niceMax(Math.max(...data.map((d) => d.cache + d.inputNet + d.output), 1));
  const slot = iw / n;
  const bw = Math.max(3, Math.min(slot - 3, 26));
  const step = xEvery ?? (n > 40 ? Math.ceil(n / 8) : n > 12 ? 5 : 2);

  const grid: ReactNode[] = [];
  const ticks = 4;
  for (let g = 0; g <= ticks; g++) {
    const gv = (max * g) / ticks;
    const y = T + ih - (ih * g) / ticks;
    grid.push(<line key={`l${g}`} x1={L} x2={W - R} y1={y} y2={y} stroke="#262632" strokeWidth={1} />);
    grid.push(
      <text key={`t${g}`} x={L - 8} y={y + 3.5} textAnchor="end" fontSize={10} fill="#62626e" className="tabular-nums">
        {fmt(gv)}
      </text>,
    );
  }

  return (
    <>
      <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label={ariaLabel} style={{ maxWidth: "100%" }}>
        {grid}
        {data.map((d, i) => {
          const x = L + i * slot + (slot - bw) / 2;
          const dim = selected !== undefined && d.key !== selected;
          return (
            <g key={d.key} opacity={dim ? 0.72 : 1}>
              {stackColumn(x, T + ih, bw, [
                { v: d.cache, color: SERIES.cache },
                { v: d.inputNet, color: SERIES.input },
                { v: d.output, color: SERIES.output },
              ], ih / max)}
            </g>
          );
        })}
        {data.map((d, i) => (
          <rect
            key={`h${d.key}`}
            x={L + i * slot}
            y={T}
            width={slot}
            height={ih}
            fill="transparent"
            style={{ cursor: onPick ? "pointer" : "default" }}
            onMouseEnter={(e) => show(tipFor(d, i), e)}
            onMouseMove={(e) => move(e)}
            onMouseLeave={hide}
            onClick={() => onPick?.(d.key)}
          />
        ))}
        {data.map((d, i) => {
          if (i % step !== 0 && i !== n - 1) return null;
          const x = L + i * slot + slot / 2;
          const sel = d.key === selected;
          return (
            <text key={`x${d.key}`} x={x} y={height - 8} textAnchor="middle" fontSize={10} fill={sel ? "#2dd4bf" : "#62626e"} className="tabular-nums">
              {d.label}
            </text>
          );
        })}
        {selected &&
          data.map((d, i) =>
            d.key === selected ? (
              <circle key="sel" cx={L + i * slot + slot / 2} cy={height - 1.5} r={2} fill="#2dd4bf" />
            ) : null,
          )}
      </svg>
      {tipEl}
    </>
  );
}

export function SeriesLegend() {
  return (
    <div className="ml-auto flex gap-3.5 text-[11.5px] text-muted">
      <span><i className="mr-1.5 inline-block h-2 w-2 rounded-[2.5px]" style={{ background: SERIES.cache }} />缓存读</span>
      <span><i className="mr-1.5 inline-block h-2 w-2 rounded-[2.5px]" style={{ background: SERIES.input }} />输入（非缓存）</span>
      <span><i className="mr-1.5 inline-block h-2 w-2 rounded-[2.5px]" style={{ background: SERIES.output }} />输出</span>
    </div>
  );
}
