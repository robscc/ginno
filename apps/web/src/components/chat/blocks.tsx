"use client";

import { Flag, Check, Loader2, Circle, FileText, Workflow, Link2 } from "lucide-react";
import type { WorkflowRun } from "@/lib/types";

export type Block =
  | { kind: "text"; text: string }
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

function ToolBlock({ name, content, pending }: { name: string; content: string; pending: boolean }) {
  return (
    <div className="my-1.5 rounded-md border border-line bg-base/40 px-2.5 py-1.5 font-mono text-xs text-muted">
      {pending ? (
        <span className="inline-flex items-center gap-1.5 text-faint">
          <Loader2 className="h-3 w-3 animate-spin" /> {name}…
        </span>
      ) : (
        <>
          <span className="text-faint">tool · {name} </span>
          <span className="text-green">✓</span>
          <pre className="mt-1 whitespace-pre-wrap text-faint">{content}</pre>
        </>
      )}
    </div>
  );
}

/** Blocks rendered INSIDE the assistant card (everything except refs). */
export function InnerBlocks({ blocks, streaming }: { blocks: Block[]; streaming?: boolean }) {
  return (
    <>
      {blocks.map((b, i) => {
        const last = i === blocks.length - 1;
        if (b.kind === "text")
          return (
            <span key={i} className="whitespace-pre-wrap">
              {cleanAgentText(b.text)}
              {streaming && last && (
                <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-violet" />
              )}
            </span>
          );
        if (b.kind === "widget") return <WidgetBlock key={i} kind={b.widgetKind} data={b.data} />;
        if (b.kind === "workflow") return <WorkflowBlock key={i} run={b.run} />;
        if (b.kind === "tool")
          return <ToolBlock key={i} name={b.name} content={b.content} pending={b.pending} />;
        if (b.kind === "thinking")
          return (
            <div key={i} className="my-1 text-xs italic text-faint">
              {b.text}
            </div>
          );
        return null; // refs rendered outside
      })}
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
