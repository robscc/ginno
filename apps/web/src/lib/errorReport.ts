import type { WorkflowRun, WorkflowRunEvent } from "./types";

/**
 * Error-report builder for failed workflow runs (error-localization push).
 *
 * The goal: when a run fails, one click produces a self-contained Markdown
 * diagnostic the user can paste straight into Claude Code (or any debugger)
 * without hunting through ~/.ginno themselves. Everything here is assembled
 * client-side from the run record + GET /workflow_runs/{id}/events.
 */

/** One-line human summary of an event (used in the "最近事件" section). */
export function formatEventLine(ev: WorkflowRunEvent): string {
  const kind = String(ev.kind || "");
  if (kind === "tool_call") {
    const calls = ev.calls || [];
    return `calls: ${calls.map((c) => c.name || "?").join(", ")}`;
  }
  if (kind === "tool_result") {
    const content = String(ev.content ?? "").replace(/\s+/g, " ");
    return `${ev.name || ""}: ${content.length > 200 ? content.slice(0, 200) + "…" : content}`;
  }
  if (kind === "context_write") return `keys: ${(ev.keys || []).join(", ")}`;
  if (kind === "loop_iter") return `iter ${ev.index ?? "?"}/${ev.of ?? "?"}`;
  if (kind === "branch_decision") return `→ ${String(ev.chosen ?? "")}`;
  if (kind === "error") return String(ev.error || "");
  if (kind === "interrupt") return String(ev.question || "");
  if (kind === "node_exit") return ev.status ? `status=${ev.status}` : "";
  // fallback: compact JSON of anything unexpected, capped
  try {
    const { ts: _t, run_id: _r, kind: _k, node_id: _n, ...rest } = ev;
    const s = JSON.stringify(rest);
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  } catch {
    return "";
  }
}

function fmtClock(ts: number | undefined): string {
  if (!ts) return "--:--:--";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtISO(ts: number | undefined | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

/** Resolve the failed step: error_detail.node_id first, then step status. */
export function failedStep(run: WorkflowRun): { id: string; title: string } | null {
  const nodeId = run.error_detail?.node_id ?? null;
  const step =
    run.steps.find((s) => s.id === nodeId) || run.steps.find((s) => s.status === "failed");
  if (!step) return null;
  return { id: step.id, title: step.title || step.id };
}

/**
 * Build the copy-paste diagnostic bundle. `events` may be omitted when the
 * caller has none loaded yet — the report then skips the event tail section.
 */
export function buildRunErrorReport(run: WorkflowRun, events?: WorkflowRunEvent[]): string {
  const step = failedStep(run);
  const tb = run.error_detail?.traceback || events
    ?.filter((e) => e.kind === "error")
    .map((e) => e.traceback)
    .filter(Boolean)
    .pop();

  const lines: string[] = [];
  lines.push("# Ginno Workflow Error Report");
  lines.push("");
  lines.push("> Please help me find the cause of this workflow run failure. Full diagnostics below.");
  lines.push("");
  lines.push("## Overview");
  lines.push(`- Workflow: ${run.name || "?"} (\`${run.workflow_id}\`)`);
  lines.push(`- Run ID: \`${run.id}\``);
  lines.push(`- DSL version: v${run.dsl_version ?? "?"}`);
  lines.push(`- Status: ${run.status}`);
  lines.push(`- Started: ${fmtISO(run.started)}`);
  lines.push(`- Finished: ${fmtISO(run.finished)}`);
  if (step) lines.push(`- Failed step: ${step.title} (\`${step.id}\`)`);
  lines.push("");
  lines.push("## Error");
  lines.push("```");
  lines.push(run.error || "(no error message)");
  lines.push("```");
  lines.push("");
  lines.push("## Traceback");
  lines.push("```");
  lines.push(
    tb || "(unavailable — no traceback was captured for this failure, e.g. a process restart or an older-version record. Check the sidecar logs)",
  );
  lines.push("```");
  if (events && events.length) {
    const tail = events.slice(-15);
    lines.push("");
    lines.push(`## Recent events (last ${tail.length})`);
    lines.push("```");
    for (const ev of tail) {
      const node = ev.node_id ? ` [${ev.node_id}]` : "";
      lines.push(`${fmtClock(ev.ts)} ${ev.kind || "?"}${node} ${formatEventLine(ev)}`.trimEnd());
    }
    lines.push("```");
  }
  lines.push("");
  lines.push("## Diagnostic pointers");
  lines.push(
    `- Sidecar log: \`~/.ginno/logs/sidecar.log\` (grep \`workflow_run_failed run=${run.id.slice(0, 8)}\`)`,
  );
  lines.push(`- Events file: \`~/.ginno/workflow_runs/${run.id}.events.jsonl\``);
  return lines.join("\n");
}

/** Clipboard write with a boolean outcome (WKWebView has no prompt fallback). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
