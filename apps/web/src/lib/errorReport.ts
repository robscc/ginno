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
  lines.push("# Ginno Workflow 错误报告");
  lines.push("");
  lines.push("> 请帮我定位这个 workflow 运行失败的原因。以下是完整诊断信息。");
  lines.push("");
  lines.push("## 基本信息");
  lines.push(`- 工作流: ${run.name || "?"} (\`${run.workflow_id}\`)`);
  lines.push(`- Run ID: \`${run.id}\``);
  lines.push(`- DSL 版本: v${run.dsl_version ?? "?"}`);
  lines.push(`- 状态: ${run.status}`);
  lines.push(`- 开始: ${fmtISO(run.started)}`);
  lines.push(`- 结束: ${fmtISO(run.finished)}`);
  if (step) lines.push(`- 失败步骤: ${step.title} (\`${step.id}\`)`);
  lines.push("");
  lines.push("## 错误");
  lines.push("```");
  lines.push(run.error || "（无错误信息）");
  lines.push("```");
  lines.push("");
  lines.push("## Traceback");
  lines.push("```");
  lines.push(
    tb || "（不可用——该失败未捕获堆栈，如进程重启/旧版本记录。可查 sidecar 日志）",
  );
  lines.push("```");
  if (events && events.length) {
    const tail = events.slice(-15);
    lines.push("");
    lines.push(`## 最近事件（最后 ${tail.length} 条）`);
    lines.push("```");
    for (const ev of tail) {
      const node = ev.node_id ? ` [${ev.node_id}]` : "";
      lines.push(`${fmtClock(ev.ts)} ${ev.kind || "?"}${node} ${formatEventLine(ev)}`.trimEnd());
    }
    lines.push("```");
  }
  lines.push("");
  lines.push("## 诊断提示");
  lines.push(
    `- Sidecar 日志: \`~/.ginno/logs/sidecar.log\`（grep \`workflow_run_failed run=${run.id.slice(0, 8)}\`）`,
  );
  lines.push(`- 事件文件: \`~/.ginno/workflow_runs/${run.id}.events.jsonl\``);
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
