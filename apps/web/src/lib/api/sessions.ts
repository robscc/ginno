/**
 * Session lifecycle, usage, goal, session files, plan orchestration
 * (WS uplink frames + plan confirm/cancel) and AgentTeam.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";
import type { Goal, GoalStatus, SessionMeta, SessionUsage } from "../types";

// ---- sessions ----
export async function listSessions(project_slug = "default") {
  return json<SessionMeta[]>(`${BASE}/sessions?project_slug=${project_slug}`);
}

/** P1-3 server-side search: title/id substring + pagination. Archived
 *  sessions stay searchable on purpose. */
export async function searchSessions(q: string, opts?: { limit?: number; offset?: number; project_slug?: string }) {
  const p = new URLSearchParams();
  p.set("q", q);
  if (opts?.limit != null) p.set("limit", String(opts.limit));
  if (opts?.offset != null) p.set("offset", String(opts.offset));
  if (opts?.project_slug) p.set("project_slug", opts.project_slug);
  return json<{ ok: boolean; total: number; limit: number; offset: number; sessions: SessionMeta[] }>(
    `${BASE}/sessions/search?${p.toString()}`,
  );
}

/** P1-4 batch archive / unarchive / delete. Delete skips referenced sessions
 *  and reports them in ``skipped`` instead of breaking teams/runs. */
export async function bulkSessions(action: "archive" | "unarchive" | "delete", ids: string[]) {
  return json<{ ok: boolean; done: string[]; skipped: Array<{ id: string; reason: string; references?: string[] }> }>(
    `${BASE}/sessions/bulk`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ids }),
    },
  );
}

export interface SearchMessageHit {
  session_id: string;
  message_id: string;
  role: string;
  snippet: string;
  session_title: string;
  archived: boolean;
}

/** V2 P0-1/P0-2 unified search: title hits + message-content hits.
 *  ``scope`` = all | sessions | messages | archived. */
export async function searchAll(q: string, opts?: { scope?: "all" | "sessions" | "messages" | "archived"; limit?: number; project_slug?: string }) {
  const p = new URLSearchParams();
  p.set("q", q);
  if (opts?.scope) p.set("scope", opts.scope);
  if (opts?.limit != null) p.set("limit", String(opts.limit));
  if (opts?.project_slug) p.set("project_slug", opts.project_slug);
  return json<{
    ok: boolean;
    q: string;
    scope: string;
    index: { indexing: boolean; last_build: number | null; indexed: number };
    sessions: SessionMeta[];
    messages: SearchMessageHit[];
  }>(`${BASE}/search?${p.toString()}`);
}

/** V2 P1-3: direct download URLs (the browser follows them). */
export function exportSessionUrl(id: string) {
  return `${BASE}/export/session/${encodeURIComponent(id)}?format=md`;
}
export async function exportProject(project_slug = "default") {
  return json<{ ok?: boolean; error?: string }>(`${BASE}/export/project`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_slug }),
  });
}

export interface SessionSuggestion {
  id: string;
  severity: "high" | "medium" | "low";
  title: string;
  detail: string;
  action: { type: string; [k: string]: unknown };
}

/** V2 P2-1: execution-derived, actionable tips (never for automation). */
export async function getSuggestions(session_id: string) {
  return json<{ ok: boolean; excluded?: string; suggestions: SessionSuggestion[] }>(
    `${BASE}/sessions/${encodeURIComponent(session_id)}/suggestions`,
  );
}

/** V2 P1-2: distil a session into a skill or a KB card. */
export async function distillSession(session_id: string, kind: "skill" | "kb" | "workflow") {
  return json<{ ok: boolean; kind?: string; name?: string; path?: string; hint?: string; detail?: { error?: string } }>(
    `${BASE}/sessions/${encodeURIComponent(session_id)}/distill`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    },
  );
}

export interface TeamSpec {
  name: string;
  charter_md: string;
  members: Array<{ agent_id: string; display_name: string; role: "leader" | "member"; instructions: string }>;
  first_task: string;
}

/** V2 P2-2: prompt → editable TeamSpec (no side effects). */
export async function generateTeam(prompt: string, project_slug = "default") {
  return json<{ ok: boolean; spec?: TeamSpec; detail?: { error?: string; raw?: string } }>(
    `${BASE}/teams/generate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, project_slug }),
    },
  );
}

/** V2 P2-2: create team + members + charter from a (edited) spec. */
export async function createTeamFromSpec(spec: TeamSpec & { project_slug?: string; workspace?: string }) {
  return json<{ ok: boolean; team?: unknown; member_errors?: string[]; first_task_delivered?: boolean; detail?: { error?: string } }>(
    `${BASE}/teams/from_spec`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spec),
    },
  );
}

export interface DiagnosticsIssue {
  id: string;
  severity: "high" | "medium" | "low" | "info";
  count?: number;
  detail?: string;
}

export interface DiagnosticsReport {
  ok: boolean;
  generated_at: number;
  warnings: number;
  issues: DiagnosticsIssue[];
  sections: {
    data: { issues: DiagnosticsIssue[] };
    tasks: {
      total: number; ok: number; failed: number; caught_up: number;
      latest_failure: { run_id: string; task: string; error: string; hint?: string; kind?: string } | null;
      issues: DiagnosticsIssue[];
    };
    runtime: {
      frozen: boolean; runtime_stale: boolean; uptime_s: number; loaded_sessions: number;
      issues: DiagnosticsIssue[];
    };
    contracts: { missing_routes: string[]; issues: DiagnosticsIssue[] };
  };
}

/** P4-1 built-in self-check (read-only). */
export async function getDiagnostics() {
  return json<DiagnosticsReport>(`${BASE}/diagnostics`);
}

/** 会话已读上报（服务端收件箱语义，session_unread）：前端「正在看」成立时
 * 调用——turn 在眼前跑完/点开会话/窗口回到前台。fire-and-forget，幂等。 */
export async function markSessionSeen(session_id: string) {
  return json<{ ok: boolean }>(
    `${BASE}/sessions/${encodeURIComponent(session_id)}/seen`,
    { method: "POST" },
  );
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
  // 出生即挂载(home-mount-picker-design.md §3)——后端 CreateSessionRequest 原生支持
  context_folders?: string[];
  primary_folder?: string | null;
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
      // AgentTeam 回帖标记（messages_ui 序列化，agent-team-design.md §5.2）。
      teamReply?: import("../types").TeamReplyMeta | null;
    }>;
    // Persisted turn failure (server-side); the client re-surfaces it as an
    // error card so the retry affordance survives reloads / switches.
    // i18n_key+params(事件契约 §3)随行持久化,回放时翻译。
    last_error?: {
      turn_id?: string; message?: string;
      i18n_key?: string; params?: Record<string, string | number>;
    } | null;
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

// ---- linked sessions(topbar-unification.md §3) ----
// 关联会话投影:派生(spawned/delegated/assigned/watching)+ 手动挂载
// (mounted)+ incoming(谁在派我)。面板打开 2s 轮询、徽标 5s 轮询;
// 404 = 旧 sidecar 无此端点,前端整个 chip 隐藏。
export interface LinkedMember {
  id: string;
  title: string;
  type: string;
  rel: "spawned" | "delegated" | "assigned" | "watching" | "mounted";
  live_status: "working" | "waiting" | "idle" | "done" | "failed" | "stopped" | "unknown" | "missing";
  updated: number | null;
  terminal: boolean;
}

export interface LinkedReport {
  ok: boolean;
  members: LinkedMember[];
  incoming: Array<Omit<LinkedMember, "rel"> & { rel: LinkedMember["rel"] | "watching_me" | "assigned_me" }>;
  live: number;
  error?: string;
}

export async function getLinkedSessions(id: string) {
  return json<LinkedReport>(`${BASE}/sessions/${id}/linked`);
}

export async function mountLinkedSession(id: string, target: string) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/sessions/${id}/linked`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ target }),
  });
}

export async function detachLinkedSession(id: string, target: string) {
  return json<{ ok: boolean; error?: string }>(`${BASE}/sessions/${id}/linked/${target}`, {
    method: "DELETE",
  });
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

// ---- subagent 拆分方案（P2 共享契约 2）：WS 上行帧的发送辅助 --------------
// 帧在会话 socket 上发送（ChatStream 的 engine 持有 per-session 连接），这里
// 只负责帧形状——与 /subagent 拆分命令的下行 subagent.plan 事件配对。
export type SubagentPlanFrame =
  | { type: "subagent.plan.confirm"; plan_id: string; subtasks?: import("../types").SubagentPlanSubtask[] }
  | { type: "subagent.plan.cancel"; plan_id: string };

/** 确认拆分方案。`subtasks` 传用户在卡片里编辑过的版本（缺省 = 原样采纳）；
 *  runtime 逐个 spawn，走既有 spawn 流程与 subagent.spawned 事件。 */
export function subagentPlanConfirmFrame(
  plan_id: string,
  subtasks?: import("../types").SubagentPlanSubtask[],
): SubagentPlanFrame {
  return subtasks?.length
    ? { type: "subagent.plan.confirm", plan_id, subtasks }
    : { type: "subagent.plan.confirm", plan_id };
}

/** 丢弃拆分方案（卡片上的「取消」）。 */
export function subagentPlanCancelFrame(plan_id: string): SubagentPlanFrame {
  return { type: "subagent.plan.cancel", plan_id };
}
// ---- session files (Settings → 会话文件) ----
export async function listSessionFileDirs() {
  return json<{ ok: boolean; sessions: import("../types").SessionDirSummary[] }>(
    `${BASE}/session-files/dirs`,
  );
}
export async function listSessionDirFiles(
  project_slug: string,
  session_id: string,
  sub?: string,
) {
  const q = sub ? `&sub=${encodeURIComponent(sub)}` : "";
  return json<{ ok: boolean; path: string; entries: import("../types").SessionDirEntry[] }>(
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
/** 一键清理:删除所有「会话已删但文件残留」的目录(活跃会话不受影响)。 */
export async function cleanupOrphanSessionDirs() {
  return json<{
    ok: boolean;
    removed: { project_slug: string; session_id: string; bytes: number }[];
    failed: { project_slug: string; session_id: string; error: string }[];
    freed_bytes: number;
  }>(`${BASE}/session-files/cleanup-orphans`, { method: "POST", headers: H });
}

// ---- 任务清单编排（subagent-plan-orchestration-design.md §2/P3）------------
// 读面 REST + 用户侧确认/取消（写面的其余入口是 agent 的 plan 工具）。
export async function listSessionPlans(sessionId: string) {
  return json<import("../types").Plan[]>(
    `${BASE}/sessions/${sessionId}/plans`,
  );
}

export async function confirmSessionPlan(sessionId: string, planId: string) {
  return json<{ ok: boolean; error?: string; plan?: import("../types").Plan }>(
    `${BASE}/sessions/${sessionId}/plans/${planId}/confirm`,
    { method: "POST" },
  );
}

export async function cancelSessionPlan(sessionId: string, planId: string) {
  return json<{ ok?: boolean; plan?: import("../types").Plan }>(
    `${BASE}/sessions/${sessionId}/plans/${planId}/cancel`,
    { method: "POST" },
  );
}

// ---- AgentTeam (agent-team-design.md §5.1 / §6 P1) ----
// 状态一律由 runtime 的 teams.status 计算（§5.9 唯一权威来源），前端不自己判态。

export async function listTeams(project_slug?: string, client = "main") {
  const q = new URLSearchParams({ client });
  if (project_slug) q.set("project_slug", project_slug);
  return json<{ ok: boolean; teams: import("../types").Team[] }>(
    `${BASE}/teams?${q.toString()}`,
  );
}

export async function getTeam(teamId: string, client = "main") {
  return json<{ ok: boolean; team?: import("../types").Team; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}?client=${encodeURIComponent(client)}`,
  );
}

export async function createTeam(req: {
  name: string;
  project_slug?: string;
  agent_id?: string;
  workspace?: string;
  context_folders?: string[];
  primary_folder?: string | null;
}) {
  return json<{ ok: boolean; team?: import("../types").Team; error?: string }>(
    `${BASE}/teams`,
    { method: "POST", headers: H, body: JSON.stringify(req) },
  );
}

export async function patchTeam(teamId: string, patch: Record<string, unknown>) {
  return json<{ ok: boolean; team?: import("../types").Team; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}`,
    { method: "PATCH", headers: H, body: JSON.stringify(patch) },
  );
}

export async function deleteTeam(teamId: string, force = false) {
  // 防呆：runtime 拒绝删除有活 turn 的团队（点名成员）；force 才放行。
  return json<{ ok: boolean; error?: string; live?: string[] }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}${force ? "?force=1" : ""}`,
    { method: "DELETE" },
  );
}

export async function addTeamMember(
  teamId: string,
  req: { agent_id: string; display_name?: string; role?: "leader" | "member" },
) {
  return json<{
    ok: boolean;
    member?: { instance_id: string; session_id: string };
    team?: import("../types").Team;
    error?: string;
  }>(`${BASE}/teams/${encodeURIComponent(teamId)}/members`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(req),
  });
}

export async function patchTeamMember(
  teamId: string,
  instanceId: string,
  patch: { display_name?: string; role?: "leader" | "member" },
) {
  return json<{ ok: boolean; member?: unknown; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}`,
    { method: "PATCH", headers: H, body: JSON.stringify(patch) },
  );
}

export async function removeTeamMember(teamId: string, instanceId: string) {
  return json<{ ok: boolean; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}`,
    { method: "DELETE" },
  );
}

/** 用户打开了成员工作台 → done 转 idle（§3.3 seen 语义，每客户端独立记）。 */
export async function markTeamMemberSeen(
  teamId: string,
  instanceId: string,
  client = "main",
) {
  return json<{ ok: boolean; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}/seen?client=${encodeURIComponent(client)}`,
    { method: "POST" },
  );
}

/** @投递（§5.2）。返回三态回执；stalled 时不要自动重发（§5.3.3）。 */
export async function deliverToMember(
  teamId: string,
  req: { instance_id?: string; agent_id?: string; text: string; wait?: boolean },
  client = "main",
) {
  return json<import("../types").DeliverReceipt>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/deliver?client=${encodeURIComponent(client)}`,
    { method: "POST", headers: H, body: JSON.stringify(req) },
  );
}

// ---- AgentTeam 记忆分层（agent-team-design §5.4）----

/** 团队共享记忆 TEAM.md（注入团队全部成员 + 主会话的 turn）。 */
export async function getTeamMd(teamId: string) {
  return json<{ ok: boolean; text?: string; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/team-md`,
  );
}

export async function putTeamMd(teamId: string, text: string) {
  return json<{ ok: boolean; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/team-md`,
    { method: "PUT", headers: H, body: JSON.stringify({ text }) },
  );
}

/** 实例记忆 notes.md（仅注入该成员实例的 turn）。 */
export async function getMemberNotes(teamId: string, instanceId: string) {
  return json<{ ok: boolean; text?: string; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}/notes`,
  );
}

export async function putMemberNotes(teamId: string, instanceId: string, text: string) {
  return json<{ ok: boolean; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}/notes`,
    { method: "PUT", headers: H, body: JSON.stringify({ text }) },
  );
}

// ---- AgentTeam P2（2026-10-13 产品重构）：成员多会话 ----
/** 给成员开新会话（新上下文），建完即设为活跃。 */
export async function addTeamMemberSession(teamId: string, instanceId: string) {
  return json<{ ok: boolean; error?: string; session_id?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}/sessions`,
    { method: "POST" },
  );
}

/** 删成员的一条会话；最后一条被删时成员一并移除（runtime 语义）。 */
export async function deleteTeamMemberSession(
  teamId: string,
  instanceId: string,
  sessionId: string,
) {
  return json<{ ok: boolean; error?: string }>(
    `${BASE}/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(instanceId)}/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}
