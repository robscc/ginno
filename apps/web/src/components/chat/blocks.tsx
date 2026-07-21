"use client";

import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  FileText,
  Flag,
  Link2,
  Loader2,
  Sparkles,
  Workflow,
  X,
} from "lucide-react";
import type { WorkflowRun } from "@/lib/types";
import { Markdown } from "./Markdown";

export type Block =
  | { kind: "text"; text: string }
  | { kind: "image"; url: string }
  | { kind: "widget"; widgetKind: string; data: unknown }
  | { kind: "ref"; refKind: string; name: string; refId?: string }
  | { kind: "tool"; id?: string; name: string; content: string; pending: boolean }
  | { kind: "thinking"; text: string }
  | { kind: "workflow"; run: WorkflowRun };

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

function WidgetBlock({ kind, data }: { kind: string; data: unknown }) {
  if (kind === "stat_list" && data && typeof data === "object") {
    return <StatList data={data as Parameters<typeof StatList>[0]["data"]} />;
  }
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      <div className="mb-1 text-xs font-medium text-violet">widget · {kind}</div>
      <pre className="whitespace-pre-wrap text-xs text-muted">{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}

function WorkflowBlock({ run }: { run: WorkflowRun }) {
  const done = run.steps.filter((s) => s.status === "done").length;
  const total = run.steps.length;
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-sm font-medium text-txt">
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

// Tool outputs longer than this collapse to a compact header row by default;
// expanding still keeps the content in a capped, internally scrolling box.
const LONG_OUTPUT_LINES = 12;
const LONG_OUTPUT_CHARS = 600;

function ToolBlock({ name, content, pending }: { name: string; content: string; pending: boolean }) {
  const [open, setOpen] = useState(false);
  if (pending) {
    return (
      <div className="my-1.5 rounded-md border border-line bg-base/40 px-2.5 py-1.5 font-mono text-xs">
        <span className="inline-flex items-center gap-1.5 text-faint">
          <Loader2 className="h-3 w-3 animate-spin" /> {name}…
        </span>
      </div>
    );
  }
  const lineCount = content.split("\n").length;
  const isLong = lineCount > LONG_OUTPUT_LINES || content.length > LONG_OUTPUT_CHARS;
  return (
    <div className="my-1.5 overflow-hidden rounded-md border border-line bg-base/40 font-mono text-xs">
      <button
        onClick={() => isLong && setOpen((o) => !o)}
        className={`flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left ${
          isLong ? "cursor-pointer transition-colors hover:bg-card2/50" : "cursor-default"
        }`}
        title={isLong ? (open ? "收起" : "展开完整输出") : undefined}
      >
        {isLong ? (
          <ChevronRight
            className={`h-3 w-3 shrink-0 text-faint transition-transform ${open ? "rotate-90" : ""}`}
          />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className="truncate text-faint">
          tool · <span className="text-muted">{name}</span>
        </span>
        <span className="shrink-0 text-green">✓</span>
        <span className="ml-auto shrink-0 text-[10px] text-faint">
          {lineCount} 行 · {content.length} 字符
        </span>
      </button>
      {(open || !isLong) && (
        <div className={`border-t border-line/60 ${isLong ? "max-h-80 overflow-y-auto" : ""}`}>
          <pre className="whitespace-pre-wrap px-2.5 py-1.5 text-faint">{content}</pre>
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
  useEffect(() => {
    if (wasLive.current && !live) setOpen(false); // collapse when thinking finishes
    wasLive.current = live;
  }, [live]);
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
        <div className="max-h-60 overflow-y-auto border-t border-violet/15 px-3 py-2">
          <div className="whitespace-pre-wrap text-xs leading-relaxed text-muted">{text}</div>
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

/** Blocks rendered INSIDE the assistant card (everything except refs). */
export function InnerBlocks({ blocks, streaming }: { blocks: Block[]; streaming?: boolean }) {
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    const last = i === blocks.length - 1;
    if (b.kind === "image") {
      // Group consecutive images into one gallery.
      const urls: string[] = [];
      while (i < blocks.length && blocks[i].kind === "image") {
        urls.push((blocks[i] as Extract<Block, { kind: "image" }>).url);
        i++;
      }
      out.push(<ImageGallery key={key++} urls={urls} />);
      continue;
    }
    if (b.kind === "text") {
      out.push(
        <div key={key++}>
          <Markdown text={cleanAgentText(b.text)} />
          {streaming && last && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-violet" />
          )}
        </div>,
      );
    } else if (b.kind === "widget") {
      out.push(<WidgetBlock key={key++} kind={b.widgetKind} data={b.data} />);
    } else if (b.kind === "workflow") {
      out.push(<WorkflowBlock key={key++} run={b.run} />);
    } else if (b.kind === "tool") {
      out.push(<ToolBlock key={key++} name={b.name} content={b.content} pending={b.pending} />);
    } else if (b.kind === "thinking") {
      out.push(<ThinkingBlock key={key++} text={b.text} live={!!streaming && last} />);
    }
    // refs rendered outside
    i++;
  }
  return <>{out}</>;
}

/** User bubble content: attached images as a gallery, text kept verbatim. */
export function UserBlocks({ blocks }: { blocks: Block[] }) {
  const imgs = blocks
    .filter((b): b is Extract<Block, { kind: "image" }> => b.kind === "image")
    .map((b) => b.url);
  const texts = blocks
    .filter((b): b is Extract<Block, { kind: "text" }> => b.kind === "text")
    .map((b) => b.text);
  return (
    <>
      {imgs.length > 0 && <ImageGallery urls={imgs} />}
      {texts.map((t, i) => (
        <div key={i} className="whitespace-pre-wrap">
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
  return blocks.some((b) => b.kind === "tool" && b.pending);
}
