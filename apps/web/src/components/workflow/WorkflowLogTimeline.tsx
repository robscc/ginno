"use client";

import { useEffect, useRef, useState } from "react";
import { ShieldAlert } from "lucide-react";
import type { WorkflowRunEvent } from "@/lib/types";

const KIND_STYLE: Record<string, string> = {
  node_enter: "text-blue",
  node_exit: "text-green",
  tool_call: "text-violet",
  tool_result: "text-faint",
  context_write: "text-orange",
  branch_decision: "text-violet",
  loop_iter: "text-orange",
  loop_skip: "text-yellow",
  loop_cap: "text-yellow",
  error: "text-red",
  done: "text-green",
  supervisor_intervene: "text-orange",
};

// Kinds whose payload is worth expanding (full content beats a truncated guess
// when localizing a failure).
const EXPANDABLE = new Set(["tool_result", "error", "tool_call", "supervisor_intervene"]);

// P3 #11: supervisor interventions are system actions, not log lines — they get
// a dedicated card row with an action badge (coerce green / patch_dsl violet /
// abort red / skip gray) instead of the plain mono row.
const ACTION_BADGE: Record<string, string> = {
  coerce: "bg-green/15 text-green",
  patch_dsl: "bg-violet/15 text-violet",
  abort: "bg-red/15 text-red",
  skip: "bg-card2 text-faint",
};

function fmt(ev: WorkflowRunEvent): string {
  const kind = String(ev.kind || "");
  if (kind === "tool_call") {
    const calls = ev.calls || [];
    return calls.map((c) => c.name || "?").join(", ");
  }
  if (kind === "context_write") {
    const via = ev.method === "write_json" ? " · WRITE_JSON" : ev.method === "llm" ? " · 抽取" : "";
    return `keys: ${(ev.keys || []).join(", ")}${via}`;
  }
  if (kind === "branch_decision") return `→ ${String(ev.chosen ?? "")}`;
  if (kind === "error") return String(ev.error || "");
  if (kind === "loop_iter") return `iter ${ev.index ?? "?"}/${ev.of ?? "?"}`;
  if (kind === "loop_skip") return `空序列跳过（over ${String(ev.over ?? "")}）`;
  if (kind === "loop_cap") return `达到 max_iters=${ev.max_iters ?? "?"}，剩余 ${ev.remaining ?? "?"}`;
  if (kind === "supervisor_intervene") {
    const action = String(ev.action ?? "?");
    const n = Array.isArray(ev.errors) ? (ev.errors as unknown[]).length : 0;
    const reason = typeof ev.reason === "string" && ev.reason ? ` · ${ev.reason}` : "";
    return `→ ${action}${n ? ` (${n} error${n > 1 ? "s" : ""})` : ""}${reason}`;
  }
  if (kind === "tool_result") {
    const s = `${ev.name || ""} ${String(ev.content ?? "")}`;
    return s.length > 80 ? s.slice(0, 80) + "…" : s;
  }
  return "";
}

/** Full, untruncated body for an expanded row. */
function expandBody(ev: WorkflowRunEvent): string {
  const kind = String(ev.kind || "");
  if (kind === "tool_result") return String(ev.content ?? "");
  if (kind === "error") {
    return [String(ev.error || ""), ev.traceback ? `\n${ev.traceback}` : ""]
      .join("")
      .trim();
  }
  if (kind === "tool_call") {
    try {
      return JSON.stringify(ev.calls || [], null, 2);
    } catch {
      return String(ev.calls);
    }
  }
  if (kind === "supervisor_intervene") {
    const errs = Array.isArray(ev.errors) ? (ev.errors as unknown[]).join("\n") : "";
    return [
      `action: ${String(ev.action ?? "?")}`,
      errs ? `errors:\n${errs}` : "",
      typeof ev.reason === "string" && ev.reason ? `reason: ${ev.reason}` : "",
    ]
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

const FILE_TOOLS = new Set([
  "glob_files", "grep_files", "read_file", "write_file", "edit_file", "bash",
]);
type TimelineFilter = "all" | "tools" | "files" | "context";

function matchesFilter(ev: WorkflowRunEvent, f: TimelineFilter): boolean {
  if (f === "all") return true;
  const kind = String(ev.kind || "");
  if (f === "tools") return kind === "tool_call" || kind === "tool_result";
  if (f === "files") {
    if (kind === "tool_call") return (ev.calls || []).some((c) => FILE_TOOLS.has(c.name || ""));
    if (kind === "tool_result") return FILE_TOOLS.has(ev.name || "");
    return false;
  }
  if (f === "context") return kind === "context_write";
  return true;
}

export function WorkflowLogTimeline({
  events,
  filters = false,
}: {
  events: WorkflowRunEvent[];
  /** Show the 全部/工具调用/文件访问/上下文写入 quick-filter tabs (Inspector). */
  filters?: boolean;
}) {
  // Single-open accordion; default-expanded to the LAST error event so a failed
  // run shows its crime scene with zero clicks.
  const [openIdx, setOpenIdx] = useState<number | null>(null);
  const [filter, setFilter] = useState<TimelineFilter>("all");
  const touched = useRef(false);

  useEffect(() => {
    if (touched.current) return;
    let lastErr = -1;
    events.forEach((ev, i) => {
      if (ev.kind === "error") lastErr = i;
    });
    setOpenIdx(lastErr >= 0 ? lastErr : null);
  }, [events]);

  if (!events.length) {
    return <div className="py-3 text-center text-xs text-faint">暂无执行日志</div>;
  }

  // Keep original indices so openIdx stays valid across filters.
  const rows = events
    .map((ev, i) => ({ ev, i }))
    .filter(({ ev }) => matchesFilter(ev, filter));

  return (
    <div className="rounded-lg border border-line bg-base/40">
      {filters && (
        <div className="flex gap-3 border-b border-line2 px-2 pt-1.5 text-[10px]">
          {([
            ["all", "全部"],
            ["tools", "工具调用"],
            ["files", "文件访问"],
            ["context", "上下文写入"],
          ] as Array<[TimelineFilter, string]>).map(([f, label]) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`-mb-px border-b pb-1 transition-colors ${
                filter === f ? "border-violet text-violet" : "border-transparent text-faint hover:text-muted"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      <div className="max-h-64 overflow-auto p-2 font-mono text-[11px]">
        {rows.length === 0 && (
          <div className="py-2 text-center text-[11px] text-faint">该过滤下暂无事件</div>
        )}
        {rows.map(({ ev, i }) => {
        const expandable = EXPANDABLE.has(String(ev.kind));
        const open = openIdx === i;
        if (ev.kind === "supervisor_intervene") {
          const action = String(ev.action ?? "?");
          const errs = Array.isArray(ev.errors) ? (ev.errors as unknown[]) : [];
          return (
            <div key={i} className="my-1">
              <div
                className="cursor-pointer rounded border border-orange/30 bg-orange/[0.05] p-2"
                onClick={() => {
                  touched.current = true;
                  setOpenIdx((cur) => (cur === i ? null : i));
                }}
                title={ev.ts ? new Date(ev.ts * 1000).toLocaleTimeString() : undefined}
              >
                <div className="flex items-center gap-1.5">
                  <ShieldAlert className="h-3.5 w-3.5 shrink-0 text-orange" />
                  <span className="font-medium text-orange">Supervisor 干预</span>
                  {ev.node_id && <span className="text-faint">[{ev.node_id}]</span>}
                  <span
                    className={`ml-auto rounded px-1.5 py-0.5 font-sans text-[10px] font-medium ${
                      ACTION_BADGE[action] || "bg-card2 text-faint"
                    }`}
                  >
                    {action}
                  </span>
                  <span className="text-faint">{open ? "▾" : "▸"}</span>
                </div>
                {errs.length > 0 && (
                  <div className="mt-1 font-sans text-[10px] leading-snug text-faint">
                    校验错误：{errs.map(String).join("；")}
                  </div>
                )}
                {typeof ev.reason === "string" && ev.reason && (
                  <div className="mt-0.5 font-sans text-[10px] leading-snug text-muted">{ev.reason}</div>
                )}
              </div>
              {open && (
                <pre className="mb-1 mt-0.5 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-base/60 p-2 font-mono text-[10px] leading-relaxed text-muted">
                  {expandBody(ev) || "（空）"}
                </pre>
              )}
            </div>
          );
        }
        return (
          <div key={i}>
            <div
              className={`flex gap-2 py-0.5 ${expandable ? "cursor-pointer hover:bg-card2/40" : ""}`}
              onClick={
                expandable
                  ? () => {
                      touched.current = true;
                      setOpenIdx((cur) => (cur === i ? null : i));
                    }
                  : undefined
              }
              title={ev.ts ? new Date(ev.ts * 1000).toLocaleTimeString() : undefined}
            >
              <span className="w-3 shrink-0 text-faint">
                {expandable ? (open ? "▾" : "▸") : ""}
              </span>
              <span className={`shrink-0 ${KIND_STYLE[String(ev.kind)] || "text-muted"}`}>
                {String(ev.kind)}
              </span>
              <span className="shrink-0 text-faint">{ev.node_id ? `[${ev.node_id}]` : ""}</span>
              <span className="truncate text-muted">{fmt(ev)}</span>
            </div>
            {open && expandable && (
              <pre className="mb-1 mt-0.5 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-base/60 p-2 font-mono text-[10px] leading-relaxed text-muted">
                {expandBody(ev) || "（空）"}
              </pre>
            )}
          </div>
        );
        })}
      </div>
    </div>
  );
}
