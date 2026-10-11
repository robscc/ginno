"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import * as d3 from "d3";
import { BarChart3, Check, Circle, Flag, Loader2, Workflow } from "lucide-react";
import type { WorkflowRun } from "@/lib/types";
import { useChatT } from "./i18n";

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  pending: "#71717a",
  error: "#ef4444",
};

function StatusGlyph({ status }: { status?: string }) {
  const c = STATUS_COLOR[status || "pending"] || STATUS_COLOR.pending;
  if (status === "done" || status === "ok") return <Check className="h-3.5 w-3.5" style={{ color: c }} />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color: c }} />;
  return <Circle className="h-3.5 w-3.5" style={{ color: c }} />;
}

function StatList({ data }: { data: { title?: string; items?: Array<{ label: string; value?: string; status?: string }> } }) {
  // Array guard, not `|| []`: a truthy non-array (the model once sent
  // {"items": {"item": [...]}}) sails through `||` and crashes .map —
  // on the live event and again on every history replay.
  const items = Array.isArray(data?.items) ? data.items : [];
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      {data?.title && (
        <div className="mb-2 flex items-center gap-1.5 text-sm font-medium text-txt">
          <Flag className="h-3.5 w-3.5 text-violet" />
          {data.title}
        </div>
      )}
      <div className="space-y-1.5">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-2 text-sm">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: STATUS_COLOR[it.status || "pending"] }}
            />
            <span className="text-txt">{it.label}</span>
            {it.value && <span className="text-muted">— {it.value}</span>}
            <span className="ml-auto">
              <StatusGlyph status={it.status} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---- d3 chart card (render_widget kind="chart") ----
// The model emits a declarative spec ({type, title, x, y, data[], format?});
// d3 only does the math (scales/shapes) and React renders the SVG — d3 never
// touches the DOM here. Colors come from the --chart-N theme tokens
// (globals.css): a fixed CVD-validated categorical order, never cycled.

interface ChartSpec {
  type: "bar" | "line" | "area" | "pie";
  title?: string;
  x: string;
  y: string;
  data: Array<Record<string, unknown>>;
  format?: "number" | "percent" | "currency";
}

const CHART_TYPES = new Set(["bar", "line", "area", "pie"]);
const CHART_W = 560;
const CHART_H = 240;
const CHART_M = { top: 14, right: 14, bottom: 26, left: 46 };
const SERIES = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

/** Defensive parse — null falls back to the JSON-dump widget below. */
function parseChartSpec(raw: unknown): ChartSpec | null {
  if (!raw || typeof raw !== "object") return null;
  const s = raw as Partial<ChartSpec>;
  if (!s.type || !CHART_TYPES.has(s.type)) return null;
  if (typeof s.x !== "string" || !s.x || typeof s.y !== "string" || !s.y) return null;
  if (!Array.isArray(s.data)) return null;
  const xk = s.x;
  const yk = s.y;
  const rows = s.data.filter(
    (d): d is Record<string, unknown> =>
      !!d && typeof d === "object" && d[xk] != null && typeof d[yk] === "number" && isFinite(d[yk] as number),
  );
  if (!rows.length) return null;
  return {
    type: s.type,
    title: typeof s.title === "string" && s.title.trim() ? s.title.trim() : "",
    x: xk,
    y: yk,
    data: rows.slice(0, 30), // hard cap; prompt asks the model to aggregate
    format: s.format,
  };
}

function makeFormatters(format?: string) {
  if (format === "percent") {
    // Accept both fractions (0.42) and pre-scaled percentages (42).
    const fmt = (v: number) =>
      Math.abs(v) <= 1.5 ? d3.format(".1%")(v) : `${d3.format(",.1f")(v)}%`;
    return { axis: fmt, label: fmt };
  }
  if (format === "currency") {
    return { axis: d3.format("$.2~s"), label: d3.format("$,.2~f") };
  }
  return { axis: d3.format(".2~s"), label: d3.format(",.2~f") };
}

function XYChart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  const { tc } = useChatT();
  const rows = spec.data;
  const xs = rows.map((d) => String(d[spec.x]));
  const ys = rows.map((d) => Number(d[spec.y]));
  const { axis: fAxis, label: fLabel } = makeFormatters(spec.format);

  const y = d3
    .scaleLinear()
    .domain([Math.min(0, d3.min(ys) ?? 0), d3.max(ys) ?? 1])
    .nice()
    .range([CHART_H - CHART_M.bottom, CHART_M.top]);
  const x = d3
    .scaleBand<string>()
    .domain(xs)
    .range([CHART_M.left, CHART_W - CHART_M.right])
    .paddingInner(0.25)
    .paddingOuter(0.12);
  const cx = (i: number) => (x(xs[i]) ?? 0) + x.bandwidth() / 2;
  const ticks = y.ticks(4);
  const step = Math.max(1, Math.ceil(xs.length / 10)); // thin crowded x labels

  const linePath =
    d3.line<number>().x((_, i) => cx(i)).y((v) => y(v)).curve(d3.curveMonotoneX)(ys) ?? "";
  const areaPath =
    d3
      .area<number>()
      .x((_, i) => cx(i))
      .y0(y(Math.max(0, y.domain()[0])))
      .y1((v) => y(v))
      .curve(d3.curveMonotoneX)(ys) ?? "";

  return (
    <>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="h-auto w-full"
        role="img"
        aria-label={spec.title || tc("widget.chartAria")}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={CHART_M.left}
              x2={CHART_W - CHART_M.right}
              y1={y(t)}
              y2={y(t)}
              style={{ stroke: "rgb(var(--line))", strokeDasharray: "2 3" }}
            />
            <text
              x={CHART_M.left - 6}
              y={y(t)}
              dy="0.32em"
              textAnchor="end"
              fontSize={10}
              style={{ fill: "rgb(var(--muted))" }}
            >
              {fAxis(t)}
            </text>
          </g>
        ))}
        {spec.type === "bar" &&
          rows.map((_, i) => (
            <rect
              key={i}
              x={x(xs[i])}
              y={y(Math.max(0, ys[i]))}
              width={x.bandwidth()}
              height={Math.max(1, Math.abs(y(0) - y(ys[i])))}
              rx={3}
              style={{
                fill: SERIES[0],
                opacity: hover === null || hover === i ? 1 : 0.4,
                transition: "opacity 120ms",
              }}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        {spec.type === "area" && <path d={areaPath} style={{ fill: SERIES[0], opacity: 0.18 }} />}
        {(spec.type === "line" || spec.type === "area") && (
          <>
            <path d={linePath} fill="none" strokeWidth={2} style={{ stroke: SERIES[0] }} />
            {ys.map((v, i) => (
              <g key={i} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
                <circle cx={cx(i)} cy={y(v)} r={9} fill="transparent" />
                <circle
                  cx={cx(i)}
                  cy={y(v)}
                  r={hover === i ? 4 : 2.5}
                  style={{
                    fill: SERIES[0],
                    stroke: "rgb(var(--base))",
                    strokeWidth: hover === i ? 1.5 : 0,
                  }}
                />
              </g>
            ))}
          </>
        )}
        {xs.map((v, i) =>
          i % step === 0 ? (
            <text
              key={i}
              x={cx(i)}
              y={CHART_H - 8}
              textAnchor="middle"
              fontSize={10}
              style={{ fill: "rgb(var(--muted))" }}
            >
              {v.length > 9 ? `${v.slice(0, 8)}…` : v}
            </text>
          ) : null,
        )}
      </svg>
      {hover !== null && (
        <div className="mt-1 text-xs text-muted">
          {xs[hover]} · <span className="font-semibold text-txt">{fLabel(ys[hover])}</span>
        </div>
      )}
    </>
  );
}

function PieChart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  const { tc } = useChatT();
  // Trust the model's aggregation — do not re-fold into Other (that hid
  // later render_widget calls that only expanded the tail).
  const rows = spec.data;
  const vals = rows.map((d) => Math.max(0, Number(d[spec.y])));
  const total = d3.sum(vals) || 1;
  const arcs = d3.pie<number>().sort(null)(vals);
  const R = CHART_H / 2 - 10;
  const cx = CHART_W / 2;
  const cy = CHART_H / 2;
  const mkArc = (r: number) =>
    d3.arc<d3.PieArcDatum<number>>().innerRadius(0).outerRadius(r).cornerRadius(2);
  const pct = d3.format(".0%");
  const { axis: fAxis, label: fLabel } = makeFormatters(spec.format);

  return (
    <>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="h-auto w-full"
        role="img"
        aria-label={spec.title || tc("widget.chartAria")}
      >
        <g transform={`translate(${cx},${cy})`}>
          {arcs.map((a, i) => (
            <path
              key={i}
              d={(hover === i ? mkArc(R * 0.76) : mkArc(R * 0.72))(a) ?? ""}
              style={{
                fill: SERIES[i % SERIES.length],
                stroke: "rgb(var(--base))",
                strokeWidth: 2,
                opacity: hover === null || hover === i ? 1 : 0.45,
                transition: "opacity 120ms",
              }}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
          {arcs.map((a, i) => {
            const frac = vals[i] / total;
            if (frac < 0.08) return null; // direct labels only where they fit
            const [lx, ly] = mkArc(R * 0.48).centroid(a);
            return (
              <text
                key={`t${i}`}
                x={lx}
                y={ly}
                textAnchor="middle"
                dy="0.32em"
                fontSize={10}
                pointerEvents="none"
                style={{ fill: "rgb(var(--txt))" }}
              >
                {fAxis(vals[i])}
              </text>
            );
          })}
        </g>
      </svg>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
        {rows.map((r, i) => (
          <span
            key={i}
            className="flex cursor-default items-center gap-1.5 text-xs text-muted"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: SERIES[i % SERIES.length] }}
            />
            <span className={hover === i ? "text-txt" : ""}>{String(r[spec.x])}</span>
            <span className="text-faint">
              {fLabel(vals[i])} · {pct(vals[i] / total)}
            </span>
          </span>
        ))}
      </div>
    </>
  );
}

function ChartBlock({ spec }: { spec: ChartSpec }) {
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      {spec.title && (
        <div className="mb-1.5 flex items-center gap-1.5 text-sm font-medium text-txt">
          <BarChart3 className="h-3.5 w-3.5 text-violet" />
          {spec.title}
        </div>
      )}
      {spec.type === "pie" ? <PieChart spec={spec} /> : <XYChart spec={spec} />}
    </div>
  );
}

export function WidgetBlock({ kind, data }: { kind: string; data: unknown }) {
  const { tc } = useChatT();
  if (kind === "stat_list" && data && typeof data === "object") {
    return <StatList data={data as Parameters<typeof StatList>[0]["data"]} />;
  }
  if (kind === "chart") {
    const spec = parseChartSpec(data);
    if (spec) return <ChartBlock spec={spec} />;
    // invalid spec -> fall through to the JSON dump below (debuggable)
  }
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      <div className="mb-1 text-xs font-medium text-violet">{tc("widget.label", { kind })}</div>
      <pre className="whitespace-pre-wrap text-xs text-muted">{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}

export function WorkflowBlock({ run }: { run: WorkflowRun }) {
  const router = useRouter();
  const { tc } = useChatT();
  const done = run.steps.filter((s) => s.status === "done").length;
  const total = run.steps.length;
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      <div
        onClick={() => {
          // Deep-link to THIS run (same fix as RunBlocks). sessionStorage
          // carries the target past the App-Router mount/URL-commit race —
          // see the note in RunBlocks.
          const h = `#wf=${run.workflow_id}&tab=run&run=${run.id}`;
          try {
            sessionStorage.setItem("ginno:studio-deeplink", h);
          } catch {
            /* ignore */
          }
          router.push(`/workflows${h}`);
        }}
        title={tc("workflow.openDetails")}
        className="mb-2 flex cursor-pointer items-center gap-1.5 text-sm font-medium text-txt hover:text-violet"
      >
        <Workflow className="h-3.5 w-3.5 text-violet" />
        {run.name || tc("workflow.fallbackName")}
        <span className="ml-auto text-xs font-normal text-faint">
          {run.status} · {done}/{total}
        </span>
      </div>
      <div className="space-y-1">
        {run.steps.map((s) => (
          <div key={s.id} className="flex items-center gap-2 text-xs">
            <StatusGlyph status={s.status} />
            <span className={s.status === "done" ? "text-muted line-through" : "text-txt"}>
              {s.title}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
