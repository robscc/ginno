"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import * as d3 from "d3";
import {
  BarChart3,
  BookMarked,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  FileText,
  Flag,
  Globe,
  Link2,
  Loader2,
  RotateCw,
  Sparkles,
  Workflow,
  X,
} from "lucide-react";
import type { WikiPage, WorkflowRun } from "@/lib/types";
import { fileDownloadUrl } from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import { Markdown } from "./Markdown";
import { AskUserCard } from "./AskUserCard";
import { toolLabel } from "@/lib/toolLabels";

export type SourceItem = { kind: "wiki" | "web"; ref: string; note?: string };

export type Block =
  | { kind: "text"; text: string }
  // `url` is set for user uploads (data URL). Code-generated images carry a
  // file-ledger `fileId` (+ mtime for cache-busting) resolved via imageUrl().
  | { kind: "image"; url?: string; fileId?: string; name?: string; mtime?: number }
  | { kind: "file"; fileId?: string; name: string; path?: string; fileKind?: string }
  // Slash-skill invocation (/name …): the persisted HumanMessage carries the
  // SKILL.md injection; history replay folds it into this chip + user request.
  | { kind: "skill"; name: string; text?: string }
  | { kind: "widget"; widgetKind: string; data: unknown; renderId?: string }
  | { kind: "ref"; refKind: string; name: string; refId?: string }
  | { kind: "tool"; id?: string; name: string; content: string; pending: boolean; argsPreview?: string }
  // ask_user parked the turn on an ambiguity: the card REPLACES the pending
  // ask_user tool bubble (same tool-call id), and the tool's JSON result folds
  // back into status/answer on tool.end (a stop while parked → "skipped").
  // History replay rebuilds it from the checkpoint (api/messages_ui.py).
  | { kind: "question"; id?: string; question: string; header?: string;
      options: string[]; allowFreeText: boolean;
      status: "pending" | "answered" | "skipped";
      answer?: string; optionIndex?: number | null }
  | { kind: "thinking"; text: string }
  | { kind: "workflow"; run: WorkflowRun }
  // WorldState change announcements (docs/design/world-state-plan.md §7):
  // centered system rows in the transcript ("context chips").
  | { kind: "context"; text: string }
  // Answer provenance (docs/citations-design.md): wiki pages / web sources the
  // model cited. Server emits this on history replay; live text blocks are
  // parsed client-side (the trailing <ginno_citations> block is machine meta).
  | { kind: "sources"; items: SourceItem[] }
  // Mid-turn steering (docs/steering-design.md §4.2): a message the user sent
  // while the turn was running, absorbed at a tool-batch boundary. It renders
  // as a full-width band INSIDE the assistant bubble it interrupted — one turn
  // is one bubble, and the band is that injection point made visible. Claude
  // Code shows the same thing as a full-width highlight (changelog 2.1.181).
  | { kind: "steer"; text: string; steerId?: string | null; injectedAt?: number };

export type QuestionBlock = Extract<Block, { kind: "question" }>;

/** Resolve an image block to a displayable URL.

User uploads carry a self-contained data URL in `url`. Code-generated images
(bash → file ledger) carry a `fileId` served by the sidecar; the URL is built
through the BASE-aware download helper, with `?t=mtime` busting the browser
cache when a same-named image is regenerated. */
export function imageUrl(b: Extract<Block, { kind: "image" }>): string {
  if (b.url) return b.url;
  if (b.fileId) {
    const t = b.mtime ? `?t=${b.mtime}` : "";
    return `${fileDownloadUrl(b.fileId)}${t}`;
  }
  return "";
}

// Non-global: used by .test()/.match() (a /g regex there would be stateful).
const CITATION_BLOCK_RE =
  /<\s*ginno_(?:wiki_)?citations\s*>([\s\S]*?)<\s*\/\s*ginno_(?:wiki_)?citations\s*>/i;
// Global variants for replace(): strip ALL blocks, including a truncated one
// whose closing tag never arrived (model output cut off inside the block).
const CITATION_BLOCK_RE_G =
  /<\s*ginno_(?:wiki_)?citations\s*>[\s\S]*?<\s*\/\s*ginno_(?:wiki_)?citations\s*>/gi;
const CITATION_UNCLOSED_RE = /<\s*ginno_(?:wiki_)?citations\b[^>]*>[\s\S]*$/i;
const CITATION_OPEN_RE = /<\s*ginno_(?:wiki_)?citations/i;
// web_search tool output lines: "[s1] Title — host\n    https://url"
const WEB_RESULT_RE = /\[(s\d+)\][^\n]*\n\s+(https?:\/\/\S+)/g;

/** Tolerantly parse a trailing ``<ginno_citations>`` block (mirror of the
 * runtime parser — history text is already stripped, this covers live text). */
export function parseSources(text: string): SourceItem[] {
  const m = text.match(CITATION_BLOCK_RE);
  if (!m) return [];
  const items: SourceItem[] = [];
  const seen = new Set<string>();
  for (const raw of m[1].split("\n")) {
    let line = raw.trim();
    if (!line) continue;
    let note = "";
    // Brackets optional: contract is note=[…] but models often emit note=… —
    // accept both so the note splits off and web refs stay clean, openable URLs.
    const nm = line.match(/\|\s*note\s*=\s*\[?(.*?)\]?\s*$/i);
    if (nm) {
      note = nm[1].trim();
      line = line.slice(0, nm.index).trimEnd();
    }
    let kind: string, ref: string;
    const bar = line.indexOf("|");
    if (bar < 0) {
      kind = "wiki";
      ref = line;
    } else {
      kind = line.slice(0, bar).trim().toLowerCase();
      ref = line.slice(bar + 1).trim();
    }
    if ((kind !== "wiki" && kind !== "web") || !ref) continue;
    const key = `${kind}:${ref.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ kind: kind as SourceItem["kind"], ref, note });
    if (items.length >= 20) break;
  }
  return items;
}

export function stripSources(text: string): string {
  return text
    .replace(CITATION_BLOCK_RE_G, "")
    .replace(CITATION_UNCLOSED_RE, "")
    .replace(/\s+$/, "");
}

/** Build an sN → URL map from the web_search tool outputs in a block list.
 * The citation contract lets the model cite by id (`web|s3`); the id only
 * means anything next to the tool result that minted it, so resolve it here
 * (both live transcripts and history carry the tool blocks). */
export function webRefMap(blocks: Block[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const b of blocks) {
    if (b.kind !== "tool" || b.name !== "web_search" || !b.content) continue;
    WEB_RESULT_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = WEB_RESULT_RE.exec(b.content)) !== null) {
      map[m[1].toLowerCase()] = m[2];
    }
  }
  return map;
}

/** Replace `web|sN` refs with their resolved URL (leaves others untouched). */
export function resolveSourceRefs(items: SourceItem[], map: Record<string, string>): SourceItem[] {
  return items.map((s) => {
    if (s.kind === "web" && /^s\d+$/i.test(s.ref)) {
      const url = map[s.ref.toLowerCase()];
      if (url) return { ...s, ref: url };
    }
    return s;
  });
}

/** While streaming, hide an in-flight (not yet closed) citation block so the
 * raw machine text never flashes; the closed block folds into SourcesBlock. */
export function maskPartialSources(text: string): string {
  if (CITATION_BLOCK_RE.test(text)) return text;
  const m = text.match(CITATION_OPEN_RE);
  return m ? text.slice(0, m.index).replace(/\s+$/, "") : text;
}

/** Centered, de-emphasized system row for context chips. */
export function ContextBlocks({ blocks }: { blocks: Extract<Block, { kind: "context" }>[] }) {
  if (!blocks.length) return null;
  return (
    <div className="flex flex-col items-center gap-1">
      {blocks.map((b, i) => (
        <div
          key={i}
          className="max-w-[85%] whitespace-pre-wrap rounded-lg border border-line/60 bg-card/40 px-3 py-1.5 text-center text-xs leading-relaxed text-muted"
        >
          {b.text}
        </div>
      ))}
    </div>
  );
}

/** Web-hostname label for a citation ref (falls back to the ref itself). */
function hostOf(ref: string): string {
  try {
    return new URL(ref).hostname.replace(/^www\./, "");
  } catch {
    return ref;
  }
}

/** Normalize a wiki ref for matching — frontend mirror of the runtime's
 * `_norm_wiki_ref`: `./` as a SEGMENT prefix, leading slashes, optional `.md`,
 * case-insensitive (models cite paths or titles with drift). */
export function normWikiRef(ref: string): string {
  let r = ref.trim();
  while (r.startsWith("./")) r = r.slice(2);
  r = r.replace(/^\/+/, "");
  if (r.toLowerCase().endsWith(".md")) r = r.slice(0, -3);
  return r.toLowerCase();
}

/** Resolve a cited wiki ref against the KB page list (path first, then title —
 * same precedence the runtime's validate_citations uses). Null = not in KB. */
export function matchWikiPage(pages: WikiPage[], ref: string): WikiPage | null {
  const key = normWikiRef(ref);
  if (key) {
    const byPath = pages.find((p) => normWikiRef(p.path) === key);
    if (byPath) return byPath;
  }
  const titleKey = ref.trim().toLowerCase();
  return pages.find((p) => p.title.trim().toLowerCase() === titleKey) ?? null;
}

/** Answer provenance: cited wiki pages + web sources (citations-design.md §5.2).
 * Collapsed to one line; expands to a list. Web rows open in the system
 * browser (via sidecar — WKWebView won't hand off external links itself);
 * wiki rows deep-link to /kb?page=<path> (resolved against the KB list —
 * frontend-only, mirroring validate_citations' path-then-title matching).
 * Unresolvable refs (index_only citations, deleted pages, model drift)
 * degrade to the old plain row + 「未收录」 badge instead of navigating. */
export function SourcesBlock({ items }: { items: SourceItem[] }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [missed, setMissed] = useState<ReadonlySet<string>>(new Set());
  const router = useRouter();
  if (!items.length) return null;

  const openWeb = async (url: string) => {
    setBusy(url);
    try {
      const { openExternal } = await import("@/lib/runtime");
      const r = await openExternal(url);
      if (!r.ok) window.open(url, "_blank", "noopener");
    } catch {
      window.open(url, "_blank", "noopener");
    } finally {
      setBusy(null);
    }
  };

  const openWiki = async (ref: string) => {
    setBusy(ref);
    try {
      const { kbWikiList } = await import("@/lib/runtime");
      const r = await kbWikiList();
      const target = r.ok ? matchWikiPage(r.pages ?? [], ref) : null;
      if (target) {
        router.push(`/kb?page=${encodeURIComponent(target.path)}`);
      } else {
        // Only "list loaded, no match" is a real miss — a fetch error keeps
        // the row clickable (the page may exist; retry later).
        setMissed((prev) => {
          const next = new Set(prev);
          next.add(ref);
          return next;
        });
      }
    } catch {
      /* sidecar unreachable — leave the row as-is */
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-line/60 bg-card/40 text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-muted hover:text-txt"
      >
        <Link2 className="h-3.5 w-3.5 shrink-0" />
        <span>来源 · {items.length}</span>
        <ChevronDown className={`ml-auto h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 border-t border-line/50 px-2 py-1.5">
          {items.map((s, i) => {
            const isWeb = s.kind === "web" && /^https?:\/\//i.test(s.ref);
            const wikiMissed = s.kind === "wiki" && missed.has(s.ref);
            const label = s.kind === "web" ? hostOf(s.ref) : s.ref.split("/").pop() || s.ref;
            const Icon = s.kind === "web" ? Globe : BookMarked;
            return (
              <div
                key={i}
                className="flex items-start gap-2 rounded-md px-1.5 py-1 hover:bg-panel/60"
                title={s.note || s.ref}
              >
                <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${s.kind === "web" ? "text-blue" : "text-violet"}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {isWeb ? (
                      <button
                        type="button"
                        disabled={busy === s.ref}
                        onClick={() => openWeb(s.ref)}
                        className="max-w-full truncate text-left text-txt underline decoration-line underline-offset-2 hover:text-blue disabled:opacity-50"
                      >
                        {label}
                      </button>
                    ) : s.kind === "wiki" && !wikiMissed ? (
                      <button
                        type="button"
                        disabled={busy === s.ref}
                        onClick={() => openWiki(s.ref)}
                        className="max-w-full truncate text-left text-txt underline decoration-line underline-offset-2 hover:text-violet disabled:opacity-50"
                      >
                        {label}
                      </button>
                    ) : (
                      <span className="min-w-0 max-w-full truncate text-txt">{label}</span>
                    )}
                    {wikiMissed && (
                      <span className="shrink-0 rounded border border-line px-1 text-[10px] leading-4 text-faint">
                        未收录
                      </span>
                    )}
                  </div>
                  {s.note && <div className="truncate text-faint">{s.note}</div>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

type FileBlock = Extract<Block, { kind: "file" }>;

const TABLE_KINDS = new Set(["spreadsheet", "table"]);

/** Clickable file chips (user bubble + replayed history). Opens the preview. */
export function FileChips({ files }: { files: FileBlock[] }) {
  const g = useGinno();
  if (!files.length) return null;
  return (
    <div className="mb-1 flex flex-wrap gap-1.5">
      {files.map((f, i) => {
        const clickable = !!f.fileId;
        return (
          <button
            key={f.fileId ?? `${f.name}-${i}`}
            disabled={!clickable}
            onClick={() =>
              clickable &&
              g.openPreview({ id: f.fileId!, name: f.name, path: f.path ?? "", kind: f.fileKind })
            }
            title={clickable ? "点击预览" : f.path}
            className={`flex items-center gap-1.5 rounded-lg border border-line bg-card2 px-2 py-1 text-xs text-txt ${
              clickable ? "cursor-pointer hover:border-violet/50" : "cursor-default"
            }`}
          >
            <span>
              {TABLE_KINDS.has(f.fileKind ?? "") ? "📊" : f.fileKind === "image" ? "🖼️" : "📄"}
            </span>
            <span className="max-w-[220px] truncate">{f.name}</span>
          </button>
        );
      })}
    </div>
  );
}

type SkillBlock = Extract<Block, { kind: "skill" }>;

/** Slash-skill invocation chips (user bubble, replayed history). The SKILL.md
    body is model scaffolding; the user sees "/name" + their request only. */
export function SkillChips({ skills }: { skills: SkillBlock[] }) {
  if (!skills.length) return null;
  return (
    <div className="mb-1 flex flex-wrap gap-1.5">
      {skills.map((s, i) => (
        <span
          key={`${s.name}-${i}`}
          title={`已调用技能 ${s.name}`}
          className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-2 py-1 text-xs text-violet"
        >
          <Sparkles className="h-3 w-3 shrink-0" />
          <span className="max-w-[220px] truncate font-medium">/{s.name}</span>
        </span>
      ))}
    </div>
  );
}

// Strip "[attached <kind>: <name>]" patterns that the LLM sometimes repeats in its text
// (violating the "don't repeat tool results" instruction). These are shown as ref chips instead.
const ATTACHED_REF_RE = /\[attached\s+\w+:\s*[^\]]+\]/g;

function cleanAgentText(text: string): string {
  return text.replace(ATTACHED_REF_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

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
  const items = data?.items || [];
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
        aria-label={spec.title || "chart"}
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
        aria-label={spec.title || "chart"}
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

function WidgetBlock({ kind, data }: { kind: string; data: unknown }) {
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
      <div className="mb-1 text-xs font-medium text-violet">widget · {kind}</div>
      <pre className="whitespace-pre-wrap text-xs text-muted">{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}

function WorkflowBlock({ run }: { run: WorkflowRun }) {
  const router = useRouter();
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
        title="打开工作流详情"
        className="mb-2 flex cursor-pointer items-center gap-1.5 text-sm font-medium text-txt hover:text-violet"
      >
        <Workflow className="h-3.5 w-3.5 text-violet" />
        {run.name || "Workflow"}
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

function RefChip({ refKind, name }: { refKind: string; name: string }) {
  const Ic = refKind === "workflow" ? Workflow : refKind === "link" ? Link2 : FileText;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted">
      <Ic className="h-3.5 w-3.5 text-violet" />
      {refKind === "workflow" ? "Workflow: " : ""}
      {name}
    </span>
  );
}

// Every tool output is collapsible. Long outputs (over these thresholds)
// default to a compact header row and expand into a capped, internally
// scrolling box; short outputs default to expanded.
const LONG_OUTPUT_LINES = 12;
const LONG_OUTPUT_CHARS = 600;

function ToolBlock({ name, content, pending, argsPreview }: { name: string; content: string; pending: boolean; argsPreview?: string }) {
  // null = user hasn't toggled yet → default depends on output length
  // (re-evaluated once content arrives, so pending→done stays correct).
  const [open, setOpen] = useState<boolean | null>(null);
  const label = toolLabel(name);
  if (pending) {
    return (
      <div className="my-1.5 rounded-md border border-line bg-base/40 px-2.5 py-1.5 font-mono text-xs">
        <span
          className="inline-flex min-w-0 max-w-full items-center gap-1.5 text-faint"
          title={argsPreview ? `${name}\n${argsPreview}` : name}
        >
          <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
          <span className="shrink-0">{label}…</span>
          {argsPreview && <span className="truncate text-muted">· {argsPreview}</span>}
        </span>
      </div>
    );
  }
  const lineCount = content.split("\n").length;
  const isLong = lineCount > LONG_OUTPUT_LINES || content.length > LONG_OUTPUT_CHARS;
  const expanded = open ?? !isLong;
  return (
    <div className="my-1.5 overflow-hidden rounded-md border border-line bg-base/40 font-mono text-xs">
      <button
        onClick={() => setOpen(!expanded)}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-left transition-colors hover:bg-card2/50"
        title={`${name} — ${expanded ? "收起" : "展开完整输出"}`}
      >
        <ChevronRight
          className={`h-3 w-3 shrink-0 text-faint transition-transform ${expanded ? "rotate-90" : ""}`}
        />
        <span className="truncate text-faint" title={argsPreview || undefined}>
          tool · <span className="text-muted">{label}</span>
          {argsPreview && <span> · {argsPreview}</span>}
        </span>
        <span className="shrink-0 text-green">✓</span>
        <span className="ml-auto shrink-0 text-[10px] text-faint">
          {lineCount} 行 · {content.length} 字符
        </span>
      </button>
      {expanded && (
        <div className={`border-t border-line/60 ${isLong ? "max-h-80 overflow-y-auto" : ""}`}>
          <pre className="overflow-x-auto whitespace-pre-wrap px-2.5 py-1.5 text-faint">{content}</pre>
        </div>
      )}
    </div>
  );
}

/**
 * Extended-thinking panel: visually distinct (accent border + tinted bg),
 * streams with a pulsing "思考中…" header, and auto-collapses once the turn
 * completes — click to re-read the full reasoning in a capped scroll box.
 */
function ThinkingBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(true);
  const wasLive = useRef(live);
  const scrollRef = useRef<HTMLDivElement>(null);
  // "Sticky bottom": keep pinned to the newest line while thinking streams in.
  // If the user scrolls up to read earlier reasoning we stop yanking them back
  // down; scrolling to the bottom re-engages the auto-follow.
  const stickToBottom = useRef(true);

  useEffect(() => {
    if (wasLive.current && !live) setOpen(false); // collapse when thinking finishes
    wasLive.current = live;
  }, [live]);

  // Re-engage auto-follow whenever the panel is (re)opened.
  useEffect(() => {
    if (open) stickToBottom.current = true;
  }, [open]);

  // While streaming, follow the newest line unless the user scrolled away.
  useEffect(() => {
    if (!live || !open) return;
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [text, live, open]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    // "At bottom" within a small threshold so a tiny overshoot still counts.
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  return (
    <div className="my-2 overflow-hidden rounded-r-lg border-l-2 border-violet/70 bg-violet/[0.07]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        title={open ? "收起" : "展开思考过程"}
      >
        <Sparkles className={`h-3.5 w-3.5 shrink-0 text-violet ${live ? "animate-pulse" : ""}`} />
        <span className="text-xs font-medium text-violet">
          {live ? "思考中…" : "已深度思考"}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[10px] text-faint">
          {!live && <span>{text.length} 字</span>}
          <ChevronDown className={`h-3 w-3 transition-transform ${open ? "" : "-rotate-90"}`} />
        </span>
      </button>
      {open && (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="max-h-60 overflow-y-auto border-t border-violet/15 px-3 py-2"
        >
          <div className="whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">{text}</div>
        </div>
      )}
    </div>
  );
}

/** Fullscreen image viewer: ESC / backdrop click closes, ←/→ paginate. */
export function Lightbox({
  urls,
  index,
  onClose,
  onNav,
}: {
  urls: string[];
  index: number;
  onClose: () => void;
  onNav: (i: number) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" && urls.length > 1) onNav((index + 1) % urls.length);
      else if (e.key === "ArrowLeft" && urls.length > 1)
        onNav((index - 1 + urls.length) % urls.length);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, urls.length, onClose, onNav]);
  return (
    <div
      className="lightbox-in fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-6"
      onClick={onClose}
      role="dialog"
      aria-label="图片预览"
    >
      <div className="absolute right-4 top-4 flex items-center gap-3 text-xs text-white/70">
        {urls.length > 1 && (
          <span className="rounded-md bg-white/10 px-2 py-0.5">
            {index + 1} / {urls.length}
          </span>
        )}
        <button
          onClick={onClose}
          aria-label="关闭"
          className="rounded-md p-1.5 transition-colors hover:bg-white/10 hover:text-white"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      {urls.length > 1 && (
        <>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onNav((index - 1 + urls.length) % urls.length);
            }}
            aria-label="上一张"
            className="absolute left-3 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onNav((index + 1) % urls.length);
            }}
            aria-label="下一张"
            className="absolute right-3 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20"
          >
            <ChevronRight className="h-5 w-5" />
          </button>
        </>
      )}
      <img
        src={urls[index]}
        alt=""
        onClick={(e) => e.stopPropagation()}
        className="lightbox-img max-h-[85vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
      />
    </div>
  );
}

/** Thumbnail strip for one or more images; click opens the lightbox. */
export function ImageGallery({ urls }: { urls: string[] }) {
  const [lb, setLb] = useState<number | null>(null);
  if (!urls.length) return null;
  const single = urls.length === 1;
  return (
    <>
      <div className="my-2 flex flex-wrap gap-2">
        {urls.map((u, i) => (
          <button
            key={i}
            onClick={() => setLb(i)}
            className="group relative overflow-hidden rounded-lg border border-line transition-colors hover:border-line2"
            title="点击预览"
          >
            <img
              src={u}
              alt=""
              className={`object-cover transition-transform duration-200 group-hover:scale-[1.03] ${
                single ? "max-h-56 max-w-full" : "h-24 w-24"
              }`}
            />
          </button>
        ))}
      </div>
      {lb !== null && (
        <Lightbox urls={urls} index={lb} onClose={() => setLb(null)} onNav={setLb} />
      )}
    </>
  );
}

/** Blocks rendered INSIDE the assistant card (everything except refs).
 * onAnswerQuestion/questionLive thread ChatStream's ask_user resume channel
 * down to a pending question card; without them the card renders read-only. */
export function InnerBlocks({
  blocks,
  streaming,
  onAnswerQuestion,
  questionLive,
}: {
  blocks: Block[];
  streaming?: boolean;
  onAnswerQuestion?: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
  questionLive?: boolean;
}) {
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  // Resolve `web|sN` citation ids against this bubble's web_search results.
  const refMap = webRefMap(blocks);
  while (i < blocks.length) {
    const b = blocks[i];
    const last = i === blocks.length - 1;
    if (b.kind === "image") {
      // Group consecutive images into one gallery.
      const urls: string[] = [];
      while (i < blocks.length && blocks[i].kind === "image") {
        urls.push(imageUrl(blocks[i] as Extract<Block, { kind: "image" }>));
        i++;
      }
      out.push(<ImageGallery key={key++} urls={urls} />);
      continue;
    }
    if (b.kind === "text") {
      // Citation framework: fold a trailing <ginno_citations> block into a
      // SourcesBlock; while streaming, mask the in-flight (unclosed) block.
      const cited = parseSources(b.text);
      // Strip UNCONDITIONALLY: an empty block (or one whose entries all fail
      // validation) parses to zero items but is still machine metadata — with
      // a conditional strip the raw tags leak into the bubble
      // (2026-08-21: turn b1463216 emitted an empty block).
      let text = stripSources(b.text);
      if (streaming && last && !cited.length) text = maskPartialSources(text);
      out.push(
        <div key={key++}>
          <Markdown text={cleanAgentText(text)} />
          {streaming && last && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-violet" />
          )}
          {cited.length > 0 && <SourcesBlock items={resolveSourceRefs(cited, refMap)} />}
        </div>,
      );
    } else if (b.kind === "sources") {
      out.push(<SourcesBlock key={key++} items={resolveSourceRefs(b.items, refMap)} />);
    } else if (b.kind === "widget") {
      out.push(
        <WidgetBlock key={b.renderId || `w${key++}`} kind={b.widgetKind} data={b.data} />,
      );
    } else if (b.kind === "workflow") {
      out.push(<WorkflowBlock key={key++} run={b.run} />);
    } else if (b.kind === "tool") {
      out.push(<ToolBlock key={key++} name={b.name} content={b.content} pending={b.pending} argsPreview={b.argsPreview} />);
    } else if (b.kind === "question") {
      out.push(
        <AskUserCard
          key={b.id || `q${key++}`}
          block={b}
          live={questionLive}
          onAnswer={onAnswerQuestion}
        />,
      );
    } else if (b.kind === "thinking") {
      out.push(<ThinkingBlock key={key++} text={b.text} live={!!streaming && last} />);
    } else if (b.kind === "file") {
      out.push(<FileChips key={key++} files={[b]} />);
    } else if (b.kind === "steer") {
      out.push(<SteerBand key={b.steerId || `st${key++}`} block={b} />);
    }
    // refs rendered outside
    i++;
  }
  return <>{out}</>;
}

/** User bubble content: attached images as a gallery, text kept verbatim. */
/** "09:41" from the server's epoch-seconds stamp ("" when absent). */
function steerClock(at?: number): string {
  if (!at) return "";
  const d = new Date(at * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** A mid-turn steered message (docs/steering-design.md §4.2).
 *
 * Full-width band, not a right-aligned bubble: the user typed this INTO a
 * running turn, so it reads as the injection point — sitting between the tool
 * step that was running and the model's continuation — rather than as a turn
 * of its own. Used both inside the assistant bubble (history replay and live
 * absorption) and, on its own, in the "queue bar" above the composer.
 */
export function SteerBand({ block }: { block: Extract<Block, { kind: "steer" }> }) {
  const clock = steerClock(block.injectedAt);
  // Deliberately NOT the thinking block's shape or colour: that block is violet
  // with a header ROW of its own (blocks.tsx ThinkingBlock), and an early
  // version of this band copied both — it read as a second thinking panel and
  // as two lines of chrome for one line of text. Here the label is an inline
  // chip on the same row as the words, and the accent is orange: unused
  // elsewhere in the chat chrome, so it collides with nothing (violet =
  // thinking/agent, blue = web citations, green = success, yellow = needs your
  // attention, red = stop/error).
  return (
    <div className="my-1 flex items-start gap-2 rounded-r-md border-l-2 border-orange/70 bg-orange/[0.07] py-1 pl-2 pr-2">
      <span className="mt-[3px] flex shrink-0 items-center gap-1 rounded bg-orange/15 px-1 py-[1px] text-[10px] font-medium leading-none text-orange">
        <RotateCw className="h-2.5 w-2.5" aria-hidden />
        运行中注入{clock ? ` · ${clock}` : ""}
      </span>
      <div className="min-w-0 whitespace-pre-wrap break-words text-[13px] leading-snug text-txt">
        {block.text}
      </div>
    </div>
  );
}

export function UserBlocks({ blocks }: { blocks: Block[] }) {
  const files = blocks.filter((b): b is FileBlock => b.kind === "file");
  const skills = blocks.filter((b): b is SkillBlock => b.kind === "skill");
  const imgs = blocks
    .filter((b): b is Extract<Block, { kind: "image" }> => b.kind === "image")
    .map(imageUrl);
  const texts = blocks
    .filter((b): b is Extract<Block, { kind: "text" }> => b.kind === "text")
    .map((b) => b.text);
  // The user's own request rides the skill block on slash-skill turns.
  const skillTexts = skills.map((s) => s.text ?? "").filter(Boolean);
  const allTexts = [...texts, ...skillTexts];
  return (
    <>
      <FileChips files={files} />
      <SkillChips skills={skills} />
      {imgs.length > 0 && <ImageGallery urls={imgs} />}
      {allTexts.map((t, i) => (
        // break-words: pasted terminal output carries long unbroken paths —
        // without it the transcript overflows the page into horizontal scroll
        <div key={i} className="min-w-0 whitespace-pre-wrap break-words">
          {t}
        </div>
      ))}
    </>
  );
}

/** Ref chips rendered BELOW the card, matching the mock layout. */
export function RefBlocks({ blocks }: { blocks: Block[] }) {
  const refs = blocks.filter((b): b is Extract<Block, { kind: "ref" }> => b.kind === "ref");
  if (!refs.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {refs.map((r, i) => (
        <RefChip key={i} refKind={r.refKind} name={r.name} />
      ))}
    </div>
  );
}

export function hasPendingTool(blocks: Block[]): boolean {
  // A pending question card counts too: it REPLACED the ask_user tool bubble,
  // so without this the working indicator would die the moment the card lands.
  return blocks.some(
    (b) => (b.kind === "tool" && b.pending) || (b.kind === "question" && b.status === "pending"),
  );
}
