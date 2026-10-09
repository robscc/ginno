/**
 * Client for the Python sidecar (FastAPI on 127.0.0.1:8787).
 * Dev: fixed port. Release: Tauri-managed sidecar (same port for now).
 */

import type { CodeEntry, CodeGitStatus, CodeListing, CodeRead, CodeRoot } from "./codeTypes";
import { uiText } from "../i18n/uiText";
// 非 hook 层取已解析 locale（模块级镜像，provider 渲染期同步赋值）：随每个
// API 请求/WS 连接下发给 runtime（i18n-design.md §1/§5）。
import { currentLocale } from "../i18n/provider";
// Type-only import: the search contract lives next to its views (SearchView),
// which is where both QuickOpen and SearchView read it from. Erased at compile,
// so this pulls no component code into the lib.
import type {
  CodeSearchHit,
  CodeSearchMode,
  CodeSearchResult,
} from "@/components/right/code/SearchView";
import type {
  AgentConfig,
  Goal,
  GoalStatus,
  ModelConfig,
  ModelConfigRefs,
  Providers,
  SessionMeta,
  SessionUsage,
  Todo,
  UsageGrid,
  UsageOverview,
  UsageRequests,
  UsageSessions,
  VerifyResult,
} from "./types";

// The sidecar serves the static pages AND the JSON API from ONE origin (the API
// is namespaced under /api). So the client must call back to whatever origin
// served the page — never a baked-in port — otherwise a page opened on a
// non-default port (e.g. a dev sidecar on 8797) would fetch the wrong sidecar.
// NEXT_PUBLIC_RUNTIME_PORT remains an opt-in override for the rare split-origin
// dev setup (web :3000 talking to a sidecar elsewhere).
//
// Reference the env var DIRECTLY, no `typeof process` guard: Next statically
// inlines NEXT_PUBLIC_* into a string literal at compile time, and a runtime
// guard can silently discard that literal when a chunk executes without the
// process shim — exactly the trap that had split-origin dev 404ing to
// same-origin despite the port being correctly inlined (2026-09-25).
const OVERRIDE_PORT = process.env.NEXT_PUBLIC_RUNTIME_PORT;

function sameOriginBase(): string {
  if (typeof window !== "undefined") {
    return OVERRIDE_PORT
      ? `${window.location.protocol}//${window.location.hostname}:${OVERRIDE_PORT}/api`
      : `${window.location.origin}/api`;
  }
  return `http://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api`; // SSR/build fallback
}

export const BASE = sameOriginBase();

function wsBase(): string {
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return `${proto}//${host}/api/ws/sessions`;
  }
  return `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/sessions`;
}

// 每个请求下发已解析 locale（X-Ginno-Language）：runtime 的
// RequestLocaleMiddleware 读头绑定 contextvar，非法/缺失回落 settings 解析。
const H = { "Content-Type": "application/json" };

async function json<T>(input: string | URL | Request, init?: RequestInit): Promise<T> {
  const r = await fetch(input, { ...init, headers: { "X-Ginno-Language": currentLocale(), ...(init?.headers ?? {}) } });
  return (await r.json()) as T;
}

export async function health() {
  return json<{ ok: boolean; version: string }>(`${BASE}/health`);
}

// ---- sessions ----
export async function listSessions(project_slug = "default") {
  return json<SessionMeta[]>(`${BASE}/sessions?project_slug=${project_slug}`);
}

export async function createSession(req: {
  project_slug?: string;
  workspace: string;
  agent_id?: string;
  title?: string;
  icon?: string;
  provider?: string;
  model?: string;
  workflow_id?: string;
  // "quick" marks floating quick-chat sessions (floating-window-design.md §1.1)
  type?: string;
}) {
  return json<SessionMeta & { ok?: boolean; error?: string }>(`${BASE}/sessions`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug: "default", ...req }),
  });
}

export async function patchSession(id: string, patch: Partial<SessionMeta>) {
  return json<{ ok: boolean; session: SessionMeta | null; error?: string }>(`${BASE}/sessions/${id}`, {
    method: "PATCH",
    headers: H,
    body: JSON.stringify(patch),
  });
}

export async function deleteSession(id: string, cascade = false) {
  // cascade=1（subagent-design.md §5.8）：删除父会话时由后端沿 parent_session_id
  // 级联删除全部后代（先协作式停止运行中的，再删 checkpoint）。前端只负责传参，
  // 后代清点仅用于确认文案与乐观移除。
  return json<{ ok: boolean; removed: boolean }>(
    `${BASE}/sessions/${id}${cascade ? "?cascade=1" : ""}`,
    {
      method: "DELETE",
    },
  );
}

export async function getSessionHistory(id: string) {
  return json<{
    ok: boolean;
    messages: Array<{
      id?: string;
      role: "user" | "assistant";
      agentId?: string | null;
      blocks: any[];
      turnId?: string;
    }>;
    // Persisted turn failure (server-side); the client re-surfaces it as an
    // error card so the retry affordance survives reloads / switches.
    last_error?: { turn_id?: string; message?: string } | null;
  }>(`${BASE}/sessions/${id}/history`);
}

// 停止一个会话（P2 共享契约 4）。POST /api/sessions/{id}/stop → 202 { stopped: true }。
// 语义由后端定：running turn 走协作式停止；waiting subagent 级联 stop 全部运行中
// 后代；idle 会话是 no-op，同样返回 202。侧栏子会话行不持有子会话的 socket
// （socket 归 ChatStream 且只给已打开的会话开），所以停止子任务只能走 HTTP；
// 成功后的状态刷新依赖既有 subagent.status WS 事件，这里不做乐观改写。
export async function stopSession(id: string) {
  return json<{ stopped: boolean }>(`${BASE}/sessions/${id}/stop`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({}),
  });
}

// Per-session cumulative model usage (TopBar counter). The live `usage` WS
// event only fires on turns; this fetch shows a session's accumulated stats
// right after switching to it. Backed by the persistent usage log, so totals
// survive runtime restarts (usage-stats-design.md §5).
export async function getSessionUsage(id: string) {
  return json<{ ok: boolean; usage: (SessionUsage & { cache_hit_ratio?: number }) | null }>(
    `${BASE}/sessions/${id}/usage`,
  );
}

// ---- usage telemetry (usage-stats-design.md §5) ----
export async function getUsageOverview(days = 30) {
  return json<UsageOverview>(`${BASE}/usage/overview?days=${days}`);
}

export async function getUsageGrid(opts?: { from?: string; to?: string }) {
  const q = new URLSearchParams();
  if (opts?.from) q.set("from", opts.from);
  if (opts?.to) q.set("to", opts.to);
  const s = q.toString();
  return json<UsageGrid>(`${BASE}/usage/grid${s ? `?${s}` : ""}`);
}

export async function getUsageSessions(opts?: { from?: string; to?: string; sort?: string; limit?: number }) {
  const q = new URLSearchParams();
  if (opts?.from) q.set("from", opts.from);
  if (opts?.to) q.set("to", opts.to);
  if (opts?.sort) q.set("sort", opts.sort);
  if (opts?.limit) q.set("limit", String(opts.limit));
  const s = q.toString();
  return json<UsageSessions>(`${BASE}/usage/sessions${s ? `?${s}` : ""}`);
}

export async function getUsageRequests(opts?: {
  date?: string;
  provider?: string;
  model?: string;
  source?: string;
  session_id?: string;
  page?: number;
  page_size?: number;
}) {
  const q = new URLSearchParams();
  if (opts?.date) q.set("date", opts.date);
  if (opts?.provider) q.set("provider", opts.provider);
  if (opts?.model) q.set("model", opts.model);
  if (opts?.source) q.set("source", opts.source);
  if (opts?.session_id) q.set("session_id", opts.session_id);
  if (opts?.page) q.set("page", String(opts.page));
  if (opts?.page_size) q.set("page_size", String(opts.page_size));
  const s = q.toString();
  return json<UsageRequests>(`${BASE}/usage/requests${s ? `?${s}` : ""}`);
}

// ---- session goal (goal-design.md §4.4) ----
export async function getSessionGoal(id: string) {
  return json<{ ok: boolean; goal: Goal | null; error?: string }>(`${BASE}/sessions/${id}/goal`);
}

export async function setSessionGoal(
  id: string,
  body: { objective?: string; status?: GoalStatus; confirm?: boolean },
) {
  return json<{ ok: boolean; goal?: Goal; needs_confirm?: boolean; error?: string }>(
    `${BASE}/sessions/${id}/goal`,
    { method: "PUT", headers: H, body: JSON.stringify(body) },
  );
}

export async function clearSessionGoal(id: string) {
  return json<{ ok: boolean; cleared: boolean; error?: string }>(`${BASE}/sessions/${id}/goal`, {
    method: "DELETE",
  });
}

// ---- context folders (docs/context-folders-design.md) ----
export async function listFolders() {
  return json<{ ok: boolean; folders: import("./types").FolderEntry[] }>(`${BASE}/folders`);
}

export async function createFolder(data: {
  path: string;
  name?: string;
  access?: "ro" | "rw";
  load_rules?: boolean;
}) {
  return json<{
    ok: boolean;
    error?: string;
    folder?: import("./types").FolderEntry;
    probe?: import("./types").FolderProbe;
  }>(`${BASE}/folders`, { method: "POST", headers: H, body: JSON.stringify(data) });
}

export async function probeFolder(path: string) {
  return json<import("./types").FolderProbe>(`${BASE}/folders/probe`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ path }),
  });
}

export async function updateFolder(id: string, patch: Partial<import("./types").FolderEntry>) {
  return json<{ ok: boolean; error?: string; folder?: import("./types").FolderEntry }>(
    `${BASE}/folders/${id}`,
    { method: "PATCH", headers: H, body: JSON.stringify(patch) },
  );
}

export async function deleteFolder(id: string) {
  return json<{ ok: boolean; removed: boolean }>(`${BASE}/folders/${id}`, { method: "DELETE" });
}

// Replace a session's mount set (idempotent full replacement).
export async function putSessionContext(
  id: string,
  body: { folder_ids: string[]; primary_id?: string | null },
) {
  return json<{
    ok: boolean;
    error?: string;
    session?: { id: string; context_folders: string[]; primary_folder: string | null };
    context_dirs?: import("./types").ContextDirEntry[];
    primary_path?: string | null;
  }>(`${BASE}/sessions/${id}/context`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(body),
  });
}

// ---- providers ----
export async function getProviders() {
  return json<{ default_provider: string; providers: Providers }>(`${BASE}/providers`);
}

export async function putProviders(providers: Providers, default_provider?: string) {
  return json<{ ok: boolean; providers: Providers; default_provider: string }>(
    `${BASE}/providers`,
    { method: "PUT", headers: H, body: JSON.stringify({ providers, default_provider }) },
  );
}

export async function verifyProvider(id: string) {
  return json<VerifyResult>(`${BASE}/providers/${id}/verify`, { method: "POST" });
}

export async function searchProbeProvider(id: string) {
  return json<{ ok: boolean; error?: string; latency_ms?: number; text?: string }>(
    `${BASE}/providers/${id}/search_probe`,
    { method: "POST" },
  );
}

// ---- model configs (multi-provider-model-config.md §2) ----
// The settings tab's Model API section talks ONLY to these endpoints; the
// legacy /providers functions above stay exported for their other consumers
// (store, AgentsSettings, GeneralSettings, ChatStream) until the backend
// swap completes.

export async function listModelConfigs() {
  return json<{ ok: boolean; configs: ModelConfig[]; default_config: string }>(
    `${BASE}/model_configs`,
  );
}

// Full-replacement write. Deletion is a PUT with the entry removed
// client-side (no DELETE endpoint). Refusals (still default / still
// referenced) come back HTTP 200 with ok:false + refs (Q8).
export async function putModelConfigs(configs: ModelConfig[], default_config: string) {
  return json<{ ok: boolean; error?: string; refs?: ModelConfigRefs; default_config?: string }>(
    `${BASE}/model_configs`,
    { method: "PUT", headers: H, body: JSON.stringify({ configs, default_config }) },
  );
}

// Q4: a passing probe is persisted server-side immediately — the returned
// `config` IS the saved record (merge it into the local list; no extra PUT).
// For edits pass the existing id so the backend updates in place; new drafts
// omit it and get a server-generated id back. Failures are HTTP 200
// {ok:false, error, refs?}.
export async function verifyModelConfig(draft: Partial<ModelConfig>) {
  return json<
    | { ok: true; config: ModelConfig; latency_ms: number }
    | { ok: false; error: string; refs?: ModelConfigRefs }
  >(`${BASE}/model_configs/verify`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(draft),
  });
}

// ---- agents ----
export async function listAgents() {
  return json<AgentConfig[]>(`${BASE}/agents`);
}
export async function createAgent(data: Partial<AgentConfig>) {
  return json<{ ok: boolean; agent?: AgentConfig; error?: string }>(`${BASE}/agents`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function updateAgent(id: string, data: Partial<AgentConfig>) {
  return json<{ ok: boolean; agent?: AgentConfig; error?: string }>(`${BASE}/agents/${id}`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function deleteAgent(id: string) {
  return json<{ ok: boolean }>(`${BASE}/agents/${id}`, { method: "DELETE" });
}
// 服务端校验 order 必须恰好覆盖当前 agent 集合（读-改-写 settings.json）。
export async function reorderAgents(order: string[]) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/agents/reorder`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ order }),
  });
}

// ---- todos ----
export async function listTodos() {
  return json<Todo[]>(`${BASE}/todos`);
}
export async function createTodo(data: Partial<Todo>) {
  return json<{ ok: boolean; todo?: Todo; error?: string }>(`${BASE}/todos`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function updateTodo(id: string, patch: Partial<Todo>) {
  return json<{ ok: boolean; todo?: Todo; error?: string }>(`${BASE}/todos/${id}`, {
    method: "PATCH",
    headers: H,
    body: JSON.stringify(patch),
  });
}
export async function deleteTodo(id: string) {
  return json<{ ok: boolean }>(`${BASE}/todos/${id}`, { method: "DELETE" });
}

// ---- misc ----
export async function listSkills(project_slug?: string) {
  const url = new URL(`${BASE}/skills`);
  if (project_slug) url.searchParams.set("project_slug", project_slug);
  return json<import("./types").SkillSummary[]>(url);
}

// 浏览器 WebSocket API 不能发自定义头——WS 侧经 ?lang=<resolved> 下发 locale，
// runtime 中间件在无 X-Ginno-Language 头时回退读 query_string。所有 new
// WebSocket 构造统一走此 helper，别在调用点裸建（会漏 lang）。
export function openSocket(url: string): WebSocket {
  const sep = url.includes("?") ? "&" : "?";
  return new WebSocket(`${url}${sep}lang=${encodeURIComponent(currentLocale())}`);
}

export function openSessionSocket(session_id: string): WebSocket {
  return openSocket(`${wsBase()}/${session_id}`);
}

// ---- subagent 拆分方案（P2 共享契约 2）：WS 上行帧的发送辅助 --------------
// 帧在会话 socket 上发送（ChatStream 的 engine 持有 per-session 连接），这里
// 只负责帧形状——与 /subagent 拆分命令的下行 subagent.plan 事件配对。
export type SubagentPlanFrame =
  | { type: "subagent.plan.confirm"; plan_id: string; subtasks?: import("./types").SubagentPlanSubtask[] }
  | { type: "subagent.plan.cancel"; plan_id: string };

/** 确认拆分方案。`subtasks` 传用户在卡片里编辑过的版本（缺省 = 原样采纳）；
 *  runtime 逐个 spawn，走既有 spawn 流程与 subagent.spawned 事件。 */
export function subagentPlanConfirmFrame(
  plan_id: string,
  subtasks?: import("./types").SubagentPlanSubtask[],
): SubagentPlanFrame {
  return subtasks?.length
    ? { type: "subagent.plan.confirm", plan_id, subtasks }
    : { type: "subagent.plan.confirm", plan_id };
}

/** 丢弃拆分方案（卡片上的「取消」）。 */
export function subagentPlanCancelFrame(plan_id: string): SubagentPlanFrame {
  return { type: "subagent.plan.cancel", plan_id };
}

/** Run-scoped live channel (design B P2): snapshot on connect, then
 *  run.event (payload.seq) / run.status / run.snapshot frames. */
export function openRunSocket(run_id: string): WebSocket {
  const path = `/api/ws/runs/${run_id}`;
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return openSocket(`${proto}//${host}${path}`);
  }
  return openSocket(`ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}${path}`);
}

// ---- workflows ----
export async function listWorkflows() {
  return json<import("./types").WorkflowDef[]>(`${BASE}/workflows`);
}
export async function listWorkflowRuns(
  opts: { workflow_id?: string; status?: string; supervisor_pending?: boolean } = {},
) {
  const q = new URLSearchParams();
  if (opts.workflow_id) q.set("workflow_id", opts.workflow_id);
  if (opts.status) q.set("status", opts.status);
  if (opts.supervisor_pending) q.set("supervisor_pending", "true");
  const qs = q.toString();
  return json<import("./types").WorkflowRun[]>(`${BASE}/workflow_runs${qs ? `?${qs}` : ""}`);
}
export async function createWorkflow(
  data: Partial<import("./types").WorkflowDef> & {
    synthesis_id?: string;
    // 方案B 阶段4: importing a draft into an EXISTING recipe — applies the dsl
    // as v(N+1) of that recipe instead of creating a new one (404 when unknown).
    workflow_id?: string;
  },
) {
  return json<{ ok: boolean; workflow?: import("./types").WorkflowDef }>(`${BASE}/workflows`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function deleteWorkflow(id: string) {
  return json<{ ok: boolean }>(`${BASE}/workflows/${id}`, { method: "DELETE" });
}
export async function getWorkflow(id: string) {
  return json<{ ok: boolean; workflow: import("./types").WorkflowDef }>(`${BASE}/workflows/${id}`);
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
  return json<{ ok: boolean; workflow: import("./types").WorkflowDef }>(
    `${BASE}/workflows/${id}/rollback`,
    { method: "POST", headers: H, body: JSON.stringify({ to, commit }) },
  );
}
export async function triggerWorkflowRun(
  workflow_id: string,
  context_override?: Record<string, unknown>,
  session_id?: string,
) {
  return json<{ ok: boolean; run: import("./types").WorkflowRun }>(`${BASE}/workflow_runs`, {
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
  return json<{ ok: boolean; run?: import("./types").WorkflowRun }>(
    `${BASE}/workflow_runs/${run_id}/present`,
    { method: "POST", headers: H, body: JSON.stringify({ session_id }) },
  );
}
export async function getWorkflowRun(run_id: string) {
  return json<{ ok: boolean; run: import("./types").WorkflowRun | null }>(
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
    run?: import("./types").WorkflowRun;
    source_run_id?: string;
    detail?: string;
  }>(`${BASE}/workflow_runs/${run_id}/retry`, { method: "POST", headers: H, body: JSON.stringify({}) });
}
export async function retryWorkflowRunFromCheckpoint(run_id: string) {
  // P2: re-execute only the failed node + suffix from the persisted checkpoint.
  return json<{
    ok: boolean;
    run?: import("./types").WorkflowRun;
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
    run?: import("./types").WorkflowRun;
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
  data: Partial<import("./types").WorkflowDef> & { commit?: string },
) {
  return json<{ ok: boolean; workflow?: import("./types").WorkflowDef }>(`${BASE}/workflows/${id}`, {
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
  return json<{ ok: boolean; events: Array<import("./types").WorkflowRunEvent> }>(
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

// ---- artifacts ----
// Artifacts belong to a session: pass session_id to scope the list (the
// Artifacts panel does this); omit for all artifacts (back-compat).
export async function listArtifacts(project_slug = "default", session_id?: string) {
  const q = session_id ? `&session_id=${encodeURIComponent(session_id)}` : "";
  return json<import("./types").Artifact[]>(
    `${BASE}/artifacts?project_slug=${project_slug}${q}`,
  );
}

// Reference-only delete: removes the panel entry, never the file on disk.
export async function deleteArtifact(id: string, project_slug = "default") {
  return json<{ ok: boolean }>(
    `${BASE}/artifacts/${id}?project_slug=${project_slug}`,
    { method: "DELETE" },
  );
}

// Inspector payload: panel record + file facts + the exact injectable schema.
export async function getArtifactMetadata(id: string, project_slug = "default") {
  return json<import("./types").ArtifactMeta>(
    `${BASE}/artifacts/${id}/metadata?project_slug=${project_slug}`,
  );
}

// User corrections from the inspector. schema → injection override;
// file_kind → registry classification fix.
export async function updateArtifact(
  id: string,
  patch: import("./types").ArtifactPatch,
  project_slug = "default",
) {
  return json<{ ok: boolean; error?: string; artifact?: import("./types").Artifact }>(
    `${BASE}/artifacts/${id}?project_slug=${project_slug}`,
    { method: "PUT", headers: H, body: JSON.stringify(patch) },
  );
}

// ---- files (upload / preview — docs/file-parsing-research.md §7) ----
export async function uploadFile(sessionId: string, file: File) {
  const fd = new FormData();
  fd.append("session_id", sessionId);
  fd.append("file", file);
  // NOTE: no Content-Type header — the browser sets the multipart boundary.
  return json<{ ok: boolean; error?: string; file?: import("./types").FileEntry }>(
    `${BASE}/files`,
    { method: "POST", body: fd },
  );
}

export async function listFiles(project_slug = "default", session_id?: string) {
  const q = session_id ? `&session_id=${encodeURIComponent(session_id)}` : "";
  return json<import("./types").FileEntry[]>(`${BASE}/files?project_slug=${project_slug}${q}`);
}

// Attach an OS file by native path (Tauri desktop drag & drop — WKWebView can't
// expose dropped files to JS, so the shell forwards the path and the sidecar
// copies + registers it). Returns the same shape as uploadFile.
export async function attachFilePath(sessionId: string, path: string) {
  return json<{ ok: boolean; error?: string; file?: import("./types").FileEntry }>(
    `${BASE}/files/attach-path`,
    { method: "POST", headers: H, body: JSON.stringify({ session_id: sessionId, path }) },
  );
}

// Temporary telemetry for diagnosing WKWebView drag & drop (see ChatStream.addFiles).
export async function debugLog(payload: unknown) {
  try {
    await fetch(`${BASE}/debug-log`, {
      method: "POST",
      headers: H,
      body: JSON.stringify(payload),
    });
  } catch {
    /* best-effort */
  }
}

export async function getFilePreview(
  fileId: string,
  opts: { sheet?: string; offset?: number; limit?: number } = {},
) {
  const p = new URLSearchParams();
  if (opts.sheet) p.set("sheet", opts.sheet);
  p.set("offset", String(opts.offset ?? 0));
  p.set("limit", String(opts.limit ?? 100));
  return json<import("./types").FilePreview>(`${BASE}/files/${fileId}/preview?${p.toString()}`);
}

// Download the original file (fmt=raw) or export one sheet as CSV (fmt=csv).
export function fileDownloadUrl(
  fileId: string,
  opts: { fmt?: "raw" | "csv"; sheet?: string } = {},
) {
  const p = new URLSearchParams();
  if (opts.fmt && opts.fmt !== "raw") p.set("fmt", opts.fmt);
  if (opts.sheet) p.set("sheet", opts.sheet);
  const q = p.toString();
  return `${BASE}/files/${fileId}/download${q ? `?${q}` : ""}`;
}

// Browser-side save: fetch as blob → object URL → anchor click. Used in dev /
// plain browsers; the Tauri webview can't trigger downloads this way, so the
// desktop UI calls saveFileToDownloads instead.
export async function downloadFile(
  fileId: string,
  fallbackName: string,
  opts: { fmt?: "raw" | "csv"; sheet?: string } = {},
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch(fileDownloadUrl(fileId, opts), {
      headers: { "X-Ginno-Language": currentLocale() },
    });
    if (!res.ok) {
      // 通用网络层错误文案走 ui 域（非 hook 场景，uiText 同步读当前 locale）
      let msg = uiText("net.downloadFailed", { status: res.status });
      try {
        const j = (await res.json()) as { detail?: string };
        if (j.detail) msg = j.detail;
      } catch {
        /* response wasn't JSON — keep the generic message */
      }
      return { ok: false, error: msg };
    }
    const cd = res.headers.get("content-disposition") || "";
    const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    const plain = /filename="([^"]+)"/i.exec(cd);
    const name = star ? decodeURIComponent(star[1]) : (plain?.[1] ?? fallbackName);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
    return { ok: true };
  } catch {
    return { ok: false, error: uiText("net.unreachable") };
  }
}

// Desktop-side save: the sidecar copies the file (or its CSV export) into the
// OS Downloads folder and reports the destination path.
export async function saveFileToDownloads(
  fileId: string,
  opts: { fmt?: "raw" | "csv"; sheet?: string } = {},
) {
  return json<{ ok: boolean; error?: string; path?: string; name?: string }>(
    `${BASE}/files/${fileId}/save-to-downloads`,
    { method: "POST", headers: H, body: JSON.stringify(opts) },
  );
}

// Ask the sidecar to open the file with the OS default application
// (Preview for PDFs, Excel/Numbers for spreadsheets, etc.).
// Works in both desktop (Tauri) and dev (browser) modes — the sidecar always
// runs on the same host, so it can invoke the OS launcher directly.
export async function openFileExternal(fileId: string) {
  return json<{ ok: boolean; error?: string }>(
    `${BASE}/files/${fileId}/open-external`,
    { method: "POST", headers: H },
  );
}

// ---- todo providers / sync (external TODO platforms) ----
export async function listTodoProviders() {
  return json<{ ok: boolean; providers: import("./types").TodoProvider[] }>(`${BASE}/todo-providers`);
}
export async function pullTodos(provider: string) {
  return json<{ ok: boolean; error?: string; run?: { id: string } }>(`${BASE}/todos/pull`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ provider }),
  });
}
export async function pushTodo(todoId: string, provider: string) {
  return json<{ ok: boolean; error?: string; run?: { id: string } }>(`${BASE}/todos/${todoId}/push`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ provider }),
  });
}
export async function todoSyncStatus() {
  return json<{ ok: boolean; entries: import("./types").TodoSyncEntry[] }>(`${BASE}/todos/sync-status`);
}

// ---- session files (Settings → 会话文件) ----
export async function listSessionFileDirs() {
  return json<{ ok: boolean; sessions: import("./types").SessionDirSummary[] }>(
    `${BASE}/session-files/dirs`,
  );
}
export async function listSessionDirFiles(
  project_slug: string,
  session_id: string,
  sub?: string,
) {
  const q = sub ? `&sub=${encodeURIComponent(sub)}` : "";
  return json<{ ok: boolean; path: string; entries: import("./types").SessionDirEntry[] }>(
    `${BASE}/session-files/list?project_slug=${project_slug}&session_id=${session_id}${q}`,
  );
}
export async function deleteSessionFile(project_slug: string, session_id: string, path: string) {
  return json<{ ok: boolean; error?: string; unregistered?: boolean }>(
    `${BASE}/session-files/file`,
    { method: "DELETE", headers: H, body: JSON.stringify({ project_slug, session_id, path }) },
  );
}
export async function deleteSessionDir(project_slug: string, session_id: string, path?: string) {
  return json<{ ok: boolean; error?: string; files_removed?: number }>(
    `${BASE}/session-files/dir`,
    { method: "DELETE", headers: H, body: JSON.stringify({ project_slug, session_id, path }) },
  );
}
export async function revealSessionFile(project_slug: string, session_id: string, path: string) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/session-files/reveal`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug, session_id, path }),
  });
}

// ---- settings / mcp / skills / kb ----
export async function stopDelegation(id: string) {
  return json(`${BASE}/delegations/${id}/stop`, { method: "POST" });
}

export async function getExternalAgents(): Promise<
  { name: string; installed: boolean; path: string }[]
> {
  return json(`${BASE}/external-agents`);
}

// ---- mods(Claude Code Mods 兼容层,claude-code-mods-design.md §7.5)----
// 后端由 runtime 侧并行实现;前端类型取宽容形状(字段缺失时设置页优雅降级)。
/** Per-mod compatibility counters (channel.compat_summary, design §7.5
 *  持续项): events × fired, the Python-backed op slice × ok, and the
 *  unimplemented `$` mentions the runner reported. Empty until first hello. */
export interface ModCompat {
  events?: Record<string, { registered?: boolean; fired?: number; failed?: number }>;
  ops?: Record<string, { called?: number; ok?: number }>;
  unimplemented?: Record<string, number>;
}

export interface ModInfo {
  name: string;
  version?: string;
  /** loaded | error | disabled | restarting */
  status?: string;
  /** global | project */
  source?: string;
  dir?: string;
  error?: string;
  enabled?: boolean;
  config?: Record<string, unknown>;
  /** grants 白名单(JS mods: fs.read/http/process…;classic hooks: classic 三态) */
  grants?: Record<string, unknown>;
  /** 兼容度计数(见 ModCompat);断连清零,新会话从空开始。 */
  compat?: ModCompat;
}

export interface ModsStatus {
  ok?: boolean;
  mods?: ModInfo[];
  /** GET /api/mods 的 runtime 字段 = ModChannel.availability()。引导判断看
   *  brokerPath/nodePath 是否解析得到,而不是 status(零 mod 时 status 是
   *  disabled 但二进制都正常,见 mods/channel.py availability)。 */
  runtime?: {
    status?: string;
    detail?: string;
    connected?: boolean;
    envSocket?: boolean;
    brokerPath?: string | null;
    nodePath?: string | null;
  } | null;
}

export async function listMods() {
  return json<ModsStatus>(`${BASE}/mods`);
}

export async function updateMod(
  name: string,
  patch: { enabled?: boolean; config?: Record<string, unknown>; grants?: Record<string, unknown> },
) {
  return json<ModInfo & { ok?: boolean; error?: string }>(
    `${BASE}/mods/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      headers: H,
      body: JSON.stringify(patch),
    },
  );
}

export async function validateMod(name: string) {
  return json<Record<string, unknown>>(`${BASE}/mods/validate`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ name }),
  });
}
export async function getSettings() {
  return json<Record<string, unknown>>(`${BASE}/settings`);
}
export async function putSettings(data: Record<string, unknown>) {
  return json<{ ok: boolean }>(`${BASE}/settings`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}
export type McpServerStatus = {
  name: string;
  connected: boolean;
  tools: number;
  error?: string | null;
};
export async function getMcp() {
  return json<{ servers: string[]; tools: string[]; status?: McpServerStatus[] }>(`${BASE}/mcp`);
}
export async function getMcpConfig() {
  return json<{ mcpServers: Record<string, unknown> }>(`${BASE}/mcp/config`);
}
export async function putMcp(data: unknown) {
  return json<{ ok: boolean }>(`${BASE}/mcp`, { method: "PUT", headers: H, body: JSON.stringify(data) });
}
export async function reloadMcp() {
  return json<{ ok: boolean; servers: string[] }>(`${BASE}/mcp/reload`, { method: "POST" });
}
export async function reconnectMcp() {
  return json<{ ok: boolean; status?: McpServerStatus[] }>(`${BASE}/mcp/reconnect`, { method: "POST" });
}
export async function createSkill(data: { name: string; body: string }) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/skills`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(data),
  });
}
export async function deleteSkill(name: string) {
  return json<{ ok: boolean }>(`${BASE}/skills/${name}`, { method: "DELETE" });
}
export async function importSkillsDir(path: string, overwrite = false) {
  return json<{
    ok: boolean;
    error?: string;
    scanned?: number;
    imported?: { name: string; description: string; from: string }[];
    skipped?: { name: string; reason: string }[];
    errors?: { name: string; error: string }[];
  }>(`${BASE}/skills/import-dir`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ path, overwrite }),
  });
}
export async function kbServers() {
  return json<Array<{ name: string; tools: string[] }>>(`${BASE}/kb/servers`);
}
export async function kbSearch(q: string) {
  return json<{ q: string; results: string[] }>(`${BASE}/kb/search?q=${encodeURIComponent(q)}`);
}
export async function kbList(path = "") {
  return json<{ path: string; results: string[] }>(`${BASE}/kb/list?path=${encodeURIComponent(path)}`);
}

// ---- knowledge base / LLMWiki (in-memory vault index) ----
export async function kbWikiSearch(q: string) {
  return json<{ ok: boolean; error?: string; results: import("./types").WikiSearchResult[] }>(
    `${BASE}/kb/wiki/search?q=${encodeURIComponent(q)}`,
  );
}
export async function kbWikiSearchByTag(tag: string) {
  return json<{ ok: boolean; error?: string; results: import("./types").WikiSearchResult[] }>(
    `${BASE}/kb/wiki/search?tag=${encodeURIComponent(tag)}`,
  );
}
export async function kbWikiList() {
  return json<{ ok: boolean; error?: string; pages: import("./types").WikiPage[] }>(`${BASE}/kb/wiki/list`);
}
export async function kbWikiStats() {
  return json<import("./types").WikiStats>(`${BASE}/kb/wiki/stats`);
}
export async function kbWikiReindex() {
  return json<{ ok: boolean; indexed: number; tags: string[] }>(`${BASE}/kb/wiki/index`, {
    method: "POST",
  });
}
export async function kbWikiDiscover() {
  return json<import("./types").WikiDiscover>(`${BASE}/kb/wiki/discover`);
}
export async function kbWikiRelated(title: string, top_k = 10) {
  return json<{ ok: boolean; related: import("./types").WikiRelatedItem[]; clusters: unknown[] }>(
    `${BASE}/kb/wiki/related?title=${encodeURIComponent(title)}&top_k=${top_k}`,
  );
}
export async function kbWikiOrphans() {
  return json<{ ok: boolean; pages: import("./types").WikiPage[] }>(`${BASE}/kb/wiki/orphans`);
}
export async function kbWikiPage(path = "", title = "") {
  const q = new URLSearchParams();
  if (path) q.set("path", path);
  if (title) q.set("title", title);
  return json<import("./types").WikiPageDoc>(`${BASE}/kb/wiki/page?${q.toString()}`);
}
export async function kbWikiPutPage(path: string, raw: string) {
  return json<{ ok: boolean; error?: string; path?: string }>(`${BASE}/kb/wiki/page`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify({ path, raw }),
  });
}
export async function kbWikiCreatePage(path: string, raw: string) {
  return json<{ ok: boolean; error?: string; path?: string }>(`${BASE}/kb/wiki/page`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ path, raw }),
  });
}
export async function kbWikiProbe(path: string) {
  return json<{
    ok: boolean;
    error?: string;
    vault_path?: string;
    detected?: {
      namespace: string;
      wiki_dir: string;
      raw_dir: string;
      research_dir: string;
      memory_dir: string;
      todo_dir: string;
    };
    wiki_pages?: number;
    raw_pages?: number;
    has_index?: boolean;
    total_md?: number;
  }>(`${BASE}/kb/wiki/probe?path=${encodeURIComponent(path)}`);
}
export async function kbWikiPutConfig(data: object) {
  return json<{ ok: boolean }>(`${BASE}/kb/wiki/config`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}

// ---- memory refinery (draft → review → apply; KB-aware promotion) ----
export async function getMemory() {
  return json<{ ok: boolean; content: string; pool_count: number; draft_pending: boolean; kb_usable: boolean }>(
    `${BASE}/memory`,
  );
}
export async function summarizeMemory(provider?: string) {
  return json<
    {
      ok: boolean;
      draft_exists?: boolean;
      error?: string;
      message?: string;
      skipped?: string;
    } & import("./types").MemoryDraft
  >(`${BASE}/memory/summarize`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(provider ? { provider } : {}),
  });
}
export async function getMemoryDraft() {
  return json<{ ok: boolean } & import("./types").MemoryDraft>(`${BASE}/memory/draft`);
}
export async function applyMemoryDraft(content?: string, force?: boolean) {
  return json<{ ok: boolean; error?: string; summarized_chars?: number; pool_remaining?: number }>(
    `${BASE}/memory/draft/apply`,
    {
      method: "POST",
      headers: H,
      body: JSON.stringify({ ...(content !== undefined ? { content } : {}), ...(force ? { force: true } : {}) }),
    },
  );
}
export async function discardMemoryDraft() {
  return json<{ ok: boolean }>(`${BASE}/memory/draft/discard`, { method: "POST" });
}
export async function kbPromotePreview(text: string, title?: string) {
  return json<import("./types").PromotePreview>(`${BASE}/kb/wiki/promote/preview`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ text, ...(title ? { title } : {}) }),
  });
}
export async function kbPromoteApply(path: string, raw: string, removeFromMemory?: string[]) {
  return json<{ ok: boolean; error?: string; path?: string; removed_from_memory?: number }>(
    `${BASE}/kb/wiki/promote/apply`,
    {
      method: "POST",
      headers: H,
      body: JSON.stringify({
        path,
        raw,
        ...(removeFromMemory?.length ? { remove_from_memory: removeFromMemory } : {}),
      }),
    },
  );
}

// ---- citations & web sources (docs/citations-design.md) ----
export type SourceItem = { kind: "wiki" | "web"; ref: string; note?: string };

export async function openExternal(url: string) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/open-external`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ url }),
  });
}

/** Click handler for rendered external links: WKWebView silently ignores
 *  target=_blank / window.open, so clicks route through the sidecar's
 *  /api/open-external (which launches the OS default browser, with the
 *  public-host guard). window.open stays as the fallback for a down
 *  sidecar (covers plain-browser dev, where it actually works). */
export function openLinkExternal(url: string) {
  openExternal(url)
    .then((r) => {
      if (!r.ok) window.open(url, "_blank", "noopener");
    })
    .catch(() => {
      window.open(url, "_blank", "noopener");
    });
}
export async function getCitationUsage(sort = "cited", limit = 20) {
  return json<{ ok: boolean; rows: Array<Record<string, unknown>> }>(
    `${BASE}/kb/wiki/usage?sort=${encodeURIComponent(sort)}&limit=${limit}`,
  );
}
export async function getWebUsage() {
  return json<{
    ok: boolean;
    engines: Array<{ engine: string; searches: number; hits_cited: number; cite_rate: number }>;
    top_domains: Array<{ domain: string; cited: number; fetched: number }>;
    total_searches: number;
    total_cited: number;
  }>(`${BASE}/kb/wiki/web-usage`);
}
export async function testWebSearch(engine: string) {
  return json<{ ok: boolean; results?: number; error?: string }>(`${BASE}/web/test-search`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ engine }),
  });
}
// 模型列表「从 API 拉取」: probes the provider's list-models endpoint with a
// config DRAFT (nothing is saved — id not required). HTTP 200 always;
// ok:false carries the error, same convention as verifyModelConfig.
export async function listModelsForConfig(draft: Partial<ModelConfig>) {
  return json<
    | { ok: true; models: Array<{ id: string; owned_by: string | null }>; latency_ms: number }
    | { ok: false; error: string }
  >(`${BASE}/model_configs/list_models`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(draft),
  });
}

// ---- code panel (docs/code-panel-design.md) ----
// Read-only workspace tree + file reader. The server answers with the shared
// `{ ok, ... }` envelope at HTTP 200 (same convention as folders/files), even
// on failure: a bad root, a denied path, a binary/oversized file, etc. These
// wrappers unwrap the success shape and throw CodeApiError on `ok:false` so
// callers (and the panel UI) can branch on the error code.
//
// Endpoints are addressed by `root` id + root-relative `path` only — the
// client never sends an absolute path (design §4.3).

/** Server-side error code from a failed code-endpoint call (design §4.3).
 *
 *  An append-only list: the S1 fence codes plus whatever later stages added.
 *  Kept as a plain string on the wire; this union exists so the UI's copy map
 *  is exhaustive rather than silently falling back. */
export type CodeErrorCode =
  | "unknown-root"
  | "root-missing"
  | "outside-root"
  | "denied-path"
  | "not-directory"
  | "binary"
  | "not-text"
  | "too-large"
  | "truncated"
  | "read-only-mount"
  | "git-internal"
  | "absent"
  | "conflict"
  | "search-timeout"
  // S4 search: an unknown `mode`, an empty/oversized `q`, and a regex that will
  // not compile are caller mistakes — `{ok:false}` at HTTP 200, never a 500.
  | "invalid-mode"
  | "invalid-query"
  | "invalid-regex"
  // S4 file operations.
  | "bad-name" // rejects "/", ".", "..", control chars, >255 bytes
  | "exists" // the target name is taken
  | "root-immutable" // a root may not be renamed, moved or deleted
  | "confirm-required" // non-empty dir: call again with `confirm: true`
  | "invalid-target"; // e.g. moving a directory into its own subtree

/** Thrown by the code clients when the runtime replies `ok:false`. `code`
 *  carries the server error code so the UI can pick a message; `message` is
 *  the human string (used as the Error message). Not a transport failure —
 *  fetch/network errors reject with their own Error. */
export class CodeApiError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message || code);
    this.name = "CodeApiError";
    this.code = code;
  }
}

/** Shared failure fields of the `{ ok:false, code, message }` envelope. */
type CodeFail = { ok: boolean; code?: string; message?: string };

function codeQuery(
  projectSlug: string,
  sessionId: string,
  extra: Record<string, string> = {},
): string {
  const q = new URLSearchParams({
    project_slug: projectSlug,
    session_id: sessionId,
    ...extra,
  });
  return q.toString();
}

/** List the workspace roots for a session (primary folder first, then the
 *  other mounts, then the session workspace). */
export async function listCodeRoots(projectSlug: string, sessionId: string): Promise<CodeRoot[]> {
  const r = await json<CodeFail & { roots?: CodeRoot[] }>(
    `${BASE}/code/roots?${codeQuery(projectSlug, sessionId)}`,
  );
  if (!r.ok || !r.roots) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return r.roots;
}

/** List one directory level under `root` (lazy tree: one layer per call).
 *  `path` is root-relative; `""` is the root itself. */
export async function listCodeDir(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
): Promise<CodeListing> {
  const r = await json<
    CodeFail & { root?: string; path?: string; entries?: CodeEntry[]; truncated?: boolean }
  >(`${BASE}/code/list?${codeQuery(projectSlug, sessionId, { root, path })}`);
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return {
    root: r.root ?? root,
    path: r.path ?? path,
    entries: r.entries ?? [],
    truncated: !!r.truncated,
  };
}

/** Read a file's text. `rev` selects the HEAD blob instead of the worktree
 *  (used later for diffing). Degradations (binary / too-large / ro mount) come
 *  back as `ok:true` with `editable:false` + a `readonly_reason`, not an error;
 *  only a real refusal (denied path, outside root, …) throws. */
export async function readCodeFile(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  rev?: "worktree" | "HEAD",
): Promise<CodeRead> {
  const extra: Record<string, string> = { root, path };
  if (rev) extra.rev = rev;
  const r = await json<CodeFail & Partial<CodeRead>>(
    `${BASE}/code/read?${codeQuery(projectSlug, sessionId, extra)}`,
  );
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return {
    root: r.root ?? root,
    path: r.path ?? path,
    version: r.version ?? "",
    encoding: r.encoding ?? "utf-8",
    language: r.language ?? "plaintext",
    editable: !!r.editable,
    readonly_reason: r.readonly_reason ?? null,
    text: r.text ?? null,
    eof: r.eof !== false,
  };
}

/** URL of a file's raw bytes — for `<img src>`, which cannot use the JSON
 *  clients above (a JSON error body would render as a corrupt image, which is
 *  why the raw endpoint answers failures with a real HTTP status instead).
 *
 *  Runs the same fence as `readCodeFile`. `version` cache-busts the URL so a
 *  changed picture is re-fetched — that is what lets the server mark the
 *  response immutable. */
export function codeRawUrl(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  version?: string,
): string {
  const extra: Record<string, string> = { root, path };
  if (version) extra.v = version;
  return `${BASE}/code/raw?${codeQuery(projectSlug, sessionId, extra)}`;
}

/** A save was refused because the file changed on disk underneath it (409).
 *
 *  Carries the version currently ON DISK, so the caller can diff against it or
 *  re-save with the right baseline without a second round trip. */
export class CodeConflictError extends Error {
  readonly version: string;
  constructor(version: string, message: string) {
    super(message);
    this.name = "CodeConflictError";
    this.version = version;
  }
}

/** Save an open file.
 *
 *  `baseVersion` is the version the buffer was based on. The server compares it
 *  against disk and refuses (409) rather than silently overwriting a change it
 *  did not make — the "announce, never auto-overwrite" rule (design §6).
 *
 *  `encoding` must be the one the file was READ with: writing a GBK file back
 *  as UTF-8 would corrupt it silently.
 *
 *  Returns the file's new version. Throws `CodeConflictError` on 409, and
 *  `CodeApiError` for any other refusal. */
export async function writeCodeFile(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  args: { content: string; baseVersion: string; encoding?: string },
): Promise<string> {
  const r = await json<CodeFail & { version?: string }>(`${BASE}/code/write`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify({
      project_slug: projectSlug,
      session_id: sessionId,
      root,
      path,
      content: args.content,
      base_version: args.baseVersion,
      encoding: args.encoding ?? "utf-8",
    }),
  });
  if (r.ok && r.version) return r.version;
  if (r.code === "conflict") {
    // 409 carries the on-disk version (see the brief §3.1).
    throw new CodeConflictError(r.version ?? "", r.message ?? "File has been modified on disk");
  }
  throw new CodeApiError(r.code ?? "not-text", r.message ?? "Save failed");
}

/** Git status for one root.
 *
 *  "Not a repo" is a NORMAL answer (`is_repo: false`), not an error: the tree
 *  simply shows no decorations. Only a real refusal (unknown root, ghost
 *  session) throws. */
export async function getCodeGit(
  projectSlug: string,
  sessionId: string,
  root: string,
): Promise<CodeGitStatus> {
  const r = await json<CodeFail & Partial<CodeGitStatus>>(
    `${BASE}/code/git?${codeQuery(projectSlug, sessionId, { root })}`,
  );
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return {
    is_repo: !!r.is_repo,
    toplevel: r.toplevel ?? null,
    branch: r.branch ?? null,
    entries: r.entries ?? {},
  };
}

// ---- file operations for the code panel (S4) --------------------------------
//
// Same convention as the clients above: the runtime replies with the house
// `{ok, ...}` envelope (HTTP 200 even on failure) and a refusal becomes a
// thrown `CodeApiError` carrying the server's own code and Chinese message,
// which the tree shows inline. `searchCode` is the one exception — its
// consumer contract is "always resolve", see its own comment.

/** Raise a refusal (`ok:false`) as a `CodeApiError`. */
function assertOk(r: CodeFail): void {
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
}

/** Create a file or folder at the FULL root-relative `path` (parent + name).
 *  `kind:"file"` makes an empty file; the write endpoint cannot create one. */
export async function codeMkdir(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  kind: "file" | "dir",
): Promise<void> {
  const r = await json<CodeFail>(`${BASE}/code/mkdir`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({
      project_slug: projectSlug,
      session_id: sessionId,
      root,
      path,
      kind,
    }),
  });
  assertOk(r);
}

/** Rename one entry in place. `to` is the FULL root-relative destination path
 *  (parent + new name) — the server never re-uses the source's parent, so a
 *  bare name would land at the wrong place. */
export async function codeRename(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  to: string,
): Promise<void> {
  const r = await json<CodeFail>(`${BASE}/code/rename`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug: projectSlug, session_id: sessionId, root, path, to }),
  });
  assertOk(r);
}

/** Move one entry within its root (drag & drop). `to` is the full
 *  root-relative destination path; cross-root moves are out of scope. */
export async function codeMove(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  to: string,
): Promise<void> {
  const r = await json<CodeFail>(`${BASE}/code/move`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug: projectSlug, session_id: sessionId, root, path, to }),
  });
  assertOk(r);
}

export interface CodeDeleteResult {
  /** Immediate child count (0 for a file or an empty dir). In the `check_only`
   *  form this is the authoritative number the delete confirmation reports. */
  count: number;
  /** Server copy — the browser fallback explains it moved to the session
   *  `.trash-*` dir rather than the OS trash. */
  message?: string;
  trashPath?: string;
}

/** Delete (trash) one entry — never a permanent unlink.
 *
 *  `checkOnly` runs the server's write fence and returns the child count
 *  WITHOUT deleting. That is what lets the Tauri trash path stay safe: the
 *  Rust `code_trash` command only re-checks path containment, and
 *  `mount_access` is most-specific-match, so an `ro` mount nested inside a
 *  writable root is invisible when judged from the root alone — only the
 *  server's fence sees it (see `api/code_fsops.py`). */
export async function codeDelete(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  opts: { confirm?: boolean; confirmCount?: number; checkOnly?: boolean } = {},
): Promise<CodeDeleteResult> {
  const body: Record<string, unknown> = {
    project_slug: projectSlug,
    session_id: sessionId,
    root,
    path,
  };
  if (opts.confirm) body.confirm = true;
  if (opts.confirmCount != null) body.confirm_count = opts.confirmCount;
  if (opts.checkOnly) body.check_only = true;
  const r = await json<CodeFail & { count?: number; trash_path?: string }>(`${BASE}/code/delete`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(body),
  });
  assertOk(r);
  return { count: r.count ?? 0, message: r.message, trashPath: r.trash_path };
}

/** Search one root by name (⌘P) or content (⇧⌘F).
 *
 *  Unlike every other client here a refusal does NOT throw: it resolves with
 *  `error: {code, message}` so the injected `CodeSearchFn` stays "always
 *  resolve" and both views render the refusal in place (brief §3.4). The only
 *  rename against the wire is `elapsed_ms` → `elapsedMs`. */
export async function searchCode(
  projectSlug: string,
  sessionId: string,
  root: string,
  q: string,
  mode: CodeSearchMode,
  limit?: number,
): Promise<CodeSearchResult> {
  const extra: Record<string, string> = { root, q, mode };
  if (limit != null) extra.limit = String(limit);
  const r = await json<
    CodeFail & {
      hits?: CodeSearchHit[];
      scanned?: number;
      truncated?: boolean;
      elapsed_ms?: number;
    }
  >(`${BASE}/code/search?${codeQuery(projectSlug, sessionId, extra)}`);
  if (!r.ok) {
    return {
      hits: [],
      scanned: 0,
      truncated: false,
      elapsedMs: 0,
      error: { code: r.code ?? "unknown", message: r.message },
    };
  }
  return {
    hits: r.hits ?? [],
    scanned: r.scanned ?? 0,
    truncated: !!r.truncated,
    elapsedMs: r.elapsed_ms ?? 0,
  };
}

// ---- connectors (connector-module-design.md §5) ----

export type ConnectorStatus =
  | "not_installed"
  | "installing"
  | "connected"
  | "disconnected"
  | "error"
  | "disabled";

export interface ConnectorInfo {
  id: string;
  name: string;
  icon: string;
  description: string;
  status: ConnectorStatus;
  statusDetail: string;
  version: string | null;
  capabilities: string[];
  enabled: boolean;
  extra?: Record<string, unknown>;
  config?: Record<string, unknown>;
  configSchema?: Record<string, unknown>;
  installSteps?: InstallStep[];
}

export interface InstallStep {
  key: string;
  title: string;
  body: string;
  action?: string;   // "reveal_folder"
  copy?: string;     // value to copy to clipboard
  waitConnect?: boolean;
  // i18n 契约（i18n-design.md §3）：runtime 附带的 catalog 键（base，渲染时拼
  // .title/.body）与 ICU 占位参数；title/body 始终是英文兜底原文。
  i18n_key?: string;
  params?: Record<string, string | number>;
}

export interface ConnectorsPayload {
  connectors: ConnectorInfo[];
  aggregateDot: string; // "" | "warn" | "error"
}

export async function listConnectors(): Promise<ConnectorsPayload> {
  return json<ConnectorsPayload>(`${BASE}/connectors`);
}

export async function getConnector(id: string): Promise<ConnectorInfo> {
  return json<ConnectorInfo>(`${BASE}/connectors/${encodeURIComponent(id)}`);
}

export async function patchConnectorConfig(
  id: string,
  config: Record<string, unknown>,
): Promise<{ ok: boolean; config?: Record<string, unknown>; error?: string }> {
  return json(`${BASE}/connectors/${encodeURIComponent(id)}/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function connectorAction(
  id: string,
  action: string,
  extra: Record<string, unknown> = {},
): Promise<Record<string, unknown>> {
  return json(`${BASE}/connectors/${encodeURIComponent(id)}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...extra }),
  });
}

export async function getHandoffStatus(): Promise<{
  active: { tabId: string }[];
}> {
  return json(`${BASE}/connectors/browser/handoff`);
}

// ---- connectors: event stream (connector-module §5 push half) ----

export interface ConnectorEvent {
  type:
    | "snapshot"
    | "connector_status_changed"
    | "tool_progress"
    | "browser_fallback_used"
    | "handoff_changed"
    | "page_pushed";
  [k: string]: unknown;
}

export interface PushedPage {
  url: string;
  title: string;
  favIconUrl?: string;
  pushedAt?: number;
}

export function wsConnectorsUrl(): string {
  const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
  if (typeof window !== "undefined") {
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return `${proto}//${host}/api/ws/connectors`;
  }
  return `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/connectors`;
}

export async function getPushedPage(): Promise<{ page: PushedPage | null }> {
  return json(`${BASE}/connectors/browser/pushed-page`);
}

// ---- scheduled tasks（docs/scheduled-tasks-design.md §5.1；路由/字段名是契约）----

export async function listSchedule(): Promise<import("./types").ScheduleConfig> {
  return json(`${BASE}/schedule`);
}

// 全局开关 / 保持唤醒。keep_awake 的唯一真值在 runtime（§3.3），前端拿到返回后
// 再同步 Tauri 壳的 caffeinate。
export async function putSchedule(body: {
  enabled?: boolean;
  keep_awake?: boolean;
}): Promise<import("./types").ScheduleConfig & { ok?: boolean; error?: string }> {
  return json(`${BASE}/schedule`, { method: "PUT", headers: H, body: JSON.stringify(body) });
}

/** 新建任务。间隔 <5 分钟 / workflow 必填输入缺失 → HTTP 400 {detail}
 *  （json() 不抛错，按 summarizeSessionToDsl 的约定透出 detail）。 */
export async function createScheduleTask(
  task: Partial<import("./types").ScheduleTask>,
): Promise<import("./types").ScheduleTask & { ok?: boolean; error?: string; detail?: string }> {
  return json(`${BASE}/schedule/tasks`, { method: "POST", headers: H, body: JSON.stringify(task) });
}

export async function patchScheduleTask(
  id: string,
  patch: Partial<import("./types").ScheduleTask>,
): Promise<import("./types").ScheduleTask & { ok?: boolean; error?: string; detail?: string }> {
  return json(`${BASE}/schedule/tasks/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: H,
    body: JSON.stringify(patch),
  });
}

export async function deleteScheduleTask(id: string): Promise<{ ok?: boolean; removed?: boolean }> {
  return json(`${BASE}/schedule/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** 立即执行（manual 触发，不计入计划），返回 run_id。 */
export async function runScheduleTaskNow(
  id: string,
): Promise<{ ok?: boolean; run_id?: string; detail?: string }> {
  return json(`${BASE}/schedule/tasks/${encodeURIComponent(id)}/run`, { method: "POST" });
}

export async function listScheduleRuns(
  opts: {
    date?: string;
    task_id?: string;
    target_type?: string;
    status?: string;
    trigger?: string;
    sort?: "asc" | "desc";
    page?: number;
  } = {},
): Promise<import("./types").ScheduleRunsPage> {
  const q = new URLSearchParams();
  if (opts.date) q.set("date", opts.date);
  if (opts.task_id) q.set("task_id", opts.task_id);
  if (opts.target_type) q.set("target_type", opts.target_type);
  if (opts.status) q.set("status", opts.status);
  if (opts.trigger) q.set("trigger", opts.trigger);
  if (opts.sort) q.set("sort", opts.sort);
  if (opts.page) q.set("page", String(opts.page));
  const s = q.toString();
  return json(`${BASE}/schedule/runs${s ? `?${s}` : ""}`);
}

export async function getScheduleTimeline(
  date: string,
): Promise<import("./types").ScheduleTimeline> {
  return json(`${BASE}/schedule/timeline?date=${encodeURIComponent(date)}`);
}

// 页面打开期间的事件通道（§6）：run_started / run_finished / task_updated /
// missed → 各页自行刷新；关页即断，不轮询。URL 模式照 wsConnectorsUrl()。
export function wsScheduleUrl(): string {
  const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
  if (typeof window !== "undefined") {
    const host = OVERRIDE_PORT
      ? `${window.location.hostname}:${OVERRIDE_PORT}`
      : window.location.host;
    return `${proto}//${host}/api/ws/schedule`;
  }
  return `ws://127.0.0.1:${OVERRIDE_PORT ?? 8787}/api/ws/schedule`;
}
