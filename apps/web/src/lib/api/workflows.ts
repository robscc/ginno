/**
 * Workflows, doctor, dry-run, synthesis-case review and workflow runs.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";

// ---- workflows ----
export async function listWorkflows() {
  return json<import("../types").WorkflowDef[]>(`${BASE}/workflows`);
}
export async function listWorkflowRuns(
  opts: { workflow_id?: string; status?: string; supervisor_pending?: boolean } = {},
) {
  const q = new URLSearchParams();
  if (opts.workflow_id) q.set("workflow_id", opts.workflow_id);
  if (opts.status) q.set("status", opts.status);
  if (opts.supervisor_pending) q.set("supervisor_pending", "true");
  const qs = q.toString();
  return json<import("../types").WorkflowRun[]>(`${BASE}/workflow_runs${qs ? `?${qs}` : ""}`);
}
export async function createWorkflow(
  data: Partial<import("../types").WorkflowDef> & {
    synthesis_id?: string;
    // 方案B 阶段4: importing a draft into an EXISTING recipe — applies the dsl
    // as v(N+1) of that recipe instead of creating a new one (404 when unknown).
    workflow_id?: string;
  },
) {
  return json<{ ok: boolean; workflow?: import("../types").WorkflowDef }>(`${BASE}/workflows`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function deleteWorkflow(id: string) {
  return json<{ ok: boolean }>(`${BASE}/workflows/${id}`, { method: "DELETE" });
}
export async function getWorkflow(id: string) {
  return json<{ ok: boolean; workflow: import("../types").WorkflowDef }>(`${BASE}/workflows/${id}`);
}
export async function listWorkflowVersions(id: string) {
  return json<{
    ok: boolean;
    versions: Array<{ version: number; current: boolean; ts?: number }>;
  }>(`${BASE}/workflows/${id}/versions`);
}
export async function diffWorkflowVersions(id: string, a: number, b: number) {
  return json<{ ok: boolean; a: number; b: number; diff: string }>(
    `${BASE}/workflows/${id}/versions/diff?a=${a}&b=${b}`,
  );
}
export async function rollbackWorkflow(id: string, to: number, commit = "") {
  return json<{ ok: boolean; workflow: import("../types").WorkflowDef }>(
    `${BASE}/workflows/${id}/rollback`,
    { method: "POST", headers: H, body: JSON.stringify({ to, commit }) },
  );
}
export async function triggerWorkflowRun(
  workflow_id: string,
  context_override?: Record<string, unknown>,
  session_id?: string,
) {
  return json<{ ok: boolean; run: import("../types").WorkflowRun }>(`${BASE}/workflow_runs`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({
      workflow_id,
      context_override,
      // bind the run to the conversation so run.* events render in-chat (design A)
      session_id,
      present_in_session_id: session_id,
    }),
  });
}
export async function presentWorkflowRun(run_id: string, session_id: string) {
  // 设计B「聊天打开本次运行」: re-bind the run's presenting session so run.*
  // events stream into an existing chat (a run triggered in the Studio or
  // headless becomes followable from a conversation).
  return json<{ ok: boolean; run?: import("../types").WorkflowRun }>(
    `${BASE}/workflow_runs/${run_id}/present`,
    { method: "POST", headers: H, body: JSON.stringify({ session_id }) },
  );
}
export async function getWorkflowRun(run_id: string) {
  return json<{ ok: boolean; run: import("../types").WorkflowRun | null }>(
    `${BASE}/workflow_runs/${run_id}`,
  );
}
export async function cancelWorkflowRun(run_id: string) {
  return json<{ ok: boolean; status: string }>(`${BASE}/workflow_runs/${run_id}/cancel`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({}),
  });
}
export async function pauseWorkflowRun(run_id: string) {
  return json<{ ok: boolean; status: string }>(`${BASE}/workflow_runs/${run_id}/pause`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({}),
  });
}
export async function resumeWorkflowRun(run_id: string, value: Record<string, unknown> = {}) {
  return json<{ ok: boolean; status: string }>(`${BASE}/workflow_runs/${run_id}/resume`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(value),
  });
}
export async function decideWorkflowRun(
  run_id: string,
  decision: string,
  context_patch?: Record<string, unknown>,
) {
  return json<{ ok: boolean; status: string }>(`${BASE}/workflow_runs/${run_id}/decide`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ decision, context_patch }),
  });
}
export async function retryWorkflowRun(run_id: string) {
  return json<{
    ok: boolean;
    run?: import("../types").WorkflowRun;
    source_run_id?: string;
    detail?: string;
  }>(`${BASE}/workflow_runs/${run_id}/retry`, { method: "POST", headers: H, body: JSON.stringify({}) });
}
export async function retryWorkflowRunFromCheckpoint(run_id: string) {
  // P2: re-execute only the failed node + suffix from the persisted checkpoint.
  return json<{
    ok: boolean;
    run?: import("../types").WorkflowRun;
    source_run_id?: string;
    detail?: string;
  }>(`${BASE}/workflow_runs/${run_id}/retry_from_checkpoint`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({}),
  });
}
export async function rerunWorkflowRunFrom(run_id: string, node_id: string) {
  // Design B 屏2「从节点重跑」: fork a NEW run re-executing from an arbitrary
  // scheduled node, compiled against the source run's pinned dsl_version.
  return json<{
    ok: boolean;
    run?: import("../types").WorkflowRun;
    source_run_id?: string;
    detail?: string;
  }>(`${BASE}/workflow_runs/${run_id}/rerun_from`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ node_id }),
  });
}
export async function deleteWorkflowRun(run_id: string) {
  return json<{ ok: boolean }>(`${BASE}/workflow_runs/${run_id}`, { method: "DELETE" });
}
export async function cleanupWorkflowRuns(statuses?: string[]) {
  return json<{ ok: boolean; deleted: number }>(`${BASE}/workflow_runs/cleanup`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(statuses ? { statuses } : {}),
  });
}
export async function updateWorkflow(
  id: string,
  data: Partial<import("../types").WorkflowDef> & { commit?: string },
) {
  return json<{ ok: boolean; workflow?: import("../types").WorkflowDef }>(`${BASE}/workflows/${id}`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function getWorkflowRunEvents(
  run_id: string,
  opts: { node_id?: string; kind?: string } = {},
) {
  const q = new URLSearchParams();
  if (opts.node_id) q.set("node_id", opts.node_id);
  if (opts.kind) q.set("kind", opts.kind);
  const qs = q.toString();
  return json<{ ok: boolean; events: Array<import("../types").WorkflowRunEvent> }>(
    `${BASE}/workflow_runs/${run_id}/events${qs ? `?${qs}` : ""}`,
  );
}

// ---- workflow doctor (static dataflow lint, master-plan §4.2) ----
export async function doctorWorkflow(id: string) {
  return json<{
    ok: boolean;
    errors: Array<{ rule: string; node_id?: string; message: string }>;
    warnings: Array<{ rule: string; node_id?: string; message: string }>;
  }>(`${BASE}/workflows/${id}/doctor`);
}

// ---- workflow dry-run (stability plan P1d: zero-LLM preflight) ----
export interface DryRunFinding {
  rule: string;
  node_id?: string;
  message: string;
}
export interface DryRunResult {
  ok: boolean;
  errors: string[];
  doctor_errors: DryRunFinding[];
  warnings: DryRunFinding[];
  unreachable: string[];
  node_count?: number;
}
export async function dryRunWorkflow(dsl: Record<string, unknown>) {
  return json<DryRunResult>(`${BASE}/workflows/dry-run`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ dsl }),
  });
}

// ---- synthesis-case review (quality-plan §3.2) ----
export interface SynthesisCaseSummary {
  synthesis_id: string;
  ts?: number;
  prompt_version?: string;
  session_stats?: { messages?: number; tool_calls?: number };
  status?: string;
  fail_stage?: string;
  attempts_used?: number;
  // true while the in-process synthesis task is alive (no output.json yet).
  // After a runtime restart, unfinished cases read running=false → "未完成".
  running?: boolean;
  outcome?: {
    created?: boolean;
    workflow_id?: string;
    first_run?: { run_id?: string; status?: string; failed_node?: string };
    edit_distance?: number;
  };
}
export async function listSynthesisCases(limit = 100) {
  return json<{ ok: boolean; cases: SynthesisCaseSummary[] }>(
    `${BASE}/synthesis/cases?limit=${limit}`,
  );
}
export async function getSynthesisCase(synthesis_id: string) {
  return json<{
    ok: boolean;
    case: {
      synthesis_id: string;
      input?: { trace?: string; prompt_version?: string; provider?: string; ts?: number; session_stats?: object };
      output?: { status?: string; fail_stage?: string; dsl?: object; attempts_used?: number };
      outcome?: object;
      attempts?: Array<{ attempt: number; parse: string; validate_errors: string[]; latency_ms: number }>;
    };
  }>(`${BASE}/synthesis/cases/${synthesis_id}`);
}
export async function getSynthesisStats(days = 30) {
  return json<{
    ok: boolean;
    total: number;
    l1_generated: number;
    l2_adopted: number;
    l3_first_run_done: number;
    avg_edit_distance: number | null;
    top_fail_labels: Array<{ label: string; count: number }>;
  }>(`${BASE}/synthesis/stats?days=${days}`);
}
export async function replaySynthesis(synthesis_id: string, provider?: string) {
  return json<{
    ok: boolean;
    dsl?: Record<string, unknown>;
    errors?: string[];
    fail_stage?: string | null;
    attempts_used?: number;
    prompt_version?: string;
  }>(`${BASE}/synthesis/replay/${synthesis_id}`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(provider ? { provider } : {}),
  });
}
export async function summarizeSessionToDsl(
  session_id: string,
  provider?: string,
  last_n?: number,
  range?: { start?: number; end?: number },
) {
  // Async contract: the endpoint validates synchronously, spawns the synthesis
  // in the background and returns immediately with {ok, synthesis_id,
  // status:"started"} — the DSL arrives via synthesis.event WS frames / case
  // polling once the task finishes. Validation failures are HTTPExceptions,
  // whose body is {detail} (json() doesn't throw on HTTP errors).
  // ``range`` (方案B 阶段4): inclusive indices into the checkpoint message
  // list, as previewed by getSummarizeTrace; last_n wins when both are sent.
  return json<{
    ok?: boolean;
    synthesis_id?: string;
    status?: string;
    error?: string;
    detail?: string;
  }>(`${BASE}/workflows/summarize-from-session`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({
      session_id,
      provider,
      ...(last_n ? { last_n } : {}),
      ...(range ? { range } : {}),
    }),
  });
}

// Numbered trace rows for the「从会话导入」picker: one row per CHECKPOINT
// message — the exact indexing summarize's `range` slices (history merges
// assistant steps into bubbles, so its rows cannot be indexed).
export interface SynthesisTraceRow {
  i: number;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  name?: string;
  tools?: string[];
}
export async function getSummarizeTrace(session_id: string) {
  return json<{
    ok: boolean;
    rows: SynthesisTraceRow[];
    count: number;
  }>(`${BASE}/workflows/summarize-trace/${encodeURIComponent(session_id)}`);
}
