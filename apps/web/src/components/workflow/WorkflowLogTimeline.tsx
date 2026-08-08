"use client";

import { useEffect, useRef, useState } from "react";
import type { WorkflowRunEvent } from "@/lib/types";

const KIND_STYLE: Record<string, string> = {
  node_enter: "text-blue",
  node_exit: "text-green",
  tool_call: "text-violet",
  tool_result: "text-faint",
  context_write: "text-orange",
  branch_decision: "text-violet",
  loop_iter: "text-orange",
  error: "text-red",
  done: "text-green",
};

// Kinds whose payload is worth expanding (full content beats a truncated guess
// when localizing a failure).
const EXPANDABLE = new Set(["tool_result", "error", "tool_call"]);

function fmt(ev: WorkflowRunEvent): string {
  const kind = String(ev.kind || "");
  if (kind === "tool_call") {
    const calls = ev.calls || [];
    return calls.map((c) => c.name || "?").join(", ");
  }
  if (kind === "context_write") return `keys: ${(ev.keys || []).join(", ")}`;
  if (kind === "branch_decision") return `→ ${String(ev.chosen ?? "")}`;
  if (kind === "error") return String(ev.error || "");
  if (kind === "loop_iter") return `iter ${ev.index ?? "?"}/${ev.of ?? "?"}`;
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
  return "";
}

export function WorkflowLogTimeline({ events }: { events: WorkflowRunEvent[] }) {
  // Single-open accordion; default-expanded to the LAST error event so a failed
  // run shows its crime scene with zero clicks.
  const [openIdx, setOpenIdx] = useState<number | null>(null);
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

  return (
    <div className="max-h-64 overflow-auto rounded-lg border border-line bg-base/40 p-2 font-mono text-[11px]">
      {events.map((ev, i) => {
        const expandable = EXPANDABLE.has(String(ev.kind));
        const open = openIdx === i;
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
  );
}
