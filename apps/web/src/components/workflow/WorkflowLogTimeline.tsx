"use client";

const KIND_STYLE: Record<string, string> = {
  node_enter: "text-blue",
  node_exit: "text-green",
  tool_call: "text-violet",
  tool_result: "text-faint",
  context_write: "text-orange",
  branch_decision: "text-violet",
  error: "text-red",
  done: "text-green",
};

function fmt(ev: Record<string, unknown>): string {
  const kind = String(ev.kind || "");
  const node = ev.node_id ? ` [${ev.node_id}]` : "";
  if (kind === "tool_call") {
    const calls = (ev.calls as Array<{ name?: string }>) || [];
    return calls.map((c) => c.name || "?").join(", ");
  }
  if (kind === "context_write") return `keys: ${((ev.keys as string[]) || []).join(", ")}`;
  if (kind === "branch_decision") return `→ ${ev.chosen}`;
  if (kind === "error") return String(ev.error || "");
  if (kind === "tool_result") return `${ev.name || ""} ${(ev.content as string) || ""}`.slice(0, 80);
  return "";
}

export function WorkflowLogTimeline({ events }: { events: Array<Record<string, unknown>> }) {
  if (!events.length) {
    return <div className="py-3 text-center text-xs text-faint">暂无执行日志</div>;
  }
  return (
    <div className="max-h-64 overflow-auto rounded-lg border border-line bg-base/40 p-2 font-mono text-[11px]">
      {events.map((ev, i) => (
        <div key={i} className="flex gap-2 py-0.5">
          <span className={`shrink-0 ${KIND_STYLE[String(ev.kind)] || "text-muted"}`}>
            {String(ev.kind)}
          </span>
          <span className="shrink-0 text-faint">{ev.node_id ? `[${ev.node_id}]` : ""}</span>
          <span className="truncate text-muted">{fmt(ev)}</span>
        </div>
      ))}
    </div>
  );
}
