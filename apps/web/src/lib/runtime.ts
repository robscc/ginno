/**
 * Client for the Python sidecar (FastAPI on 127.0.0.1:8787).
 * Dev: fixed port. Release: Tauri-managed sidecar (same port for now).
 */

import type {
  AgentConfig,
  Providers,
  SessionMeta,
  Todo,
  VerifyResult,
} from "./types";

// The sidecar serves the static pages AND the JSON API from ONE origin (the API
// is namespaced under /api). So the client must call back to whatever origin
// served the page — never a baked-in port — otherwise a page opened on a
// non-default port (e.g. a dev sidecar on 8797) would fetch the wrong sidecar.
// NEXT_PUBLIC_RUNTIME_PORT remains an opt-in override for the rare split-origin
// dev setup (web :3000 talking to a sidecar elsewhere).
const OVERRIDE_PORT =
  typeof process !== "undefined" ? process.env.NEXT_PUBLIC_RUNTIME_PORT : undefined;

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

async function json<T>(input: string | URL | Request, init?: RequestInit): Promise<T> {
  const r = await fetch(input, init);
  return (await r.json()) as T;
}

const H = { "Content-Type": "application/json" };

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
}) {
  return json<SessionMeta & { ok?: boolean; error?: string }>(`${BASE}/sessions`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug: "default", ...req }),
  });
}

export async function patchSession(id: string, patch: Partial<SessionMeta>) {
  return json<{ ok: boolean; session: SessionMeta | null }>(`${BASE}/sessions/${id}`, {
    method: "PATCH",
    headers: H,
    body: JSON.stringify(patch),
  });
}

export async function deleteSession(id: string) {
  return json<{ ok: boolean; removed: boolean }>(`${BASE}/sessions/${id}`, {
    method: "DELETE",
  });
}

export async function getSessionHistory(id: string) {
  return json<{
    ok: boolean;
    messages: Array<{
      id?: string;
      role: "user" | "assistant";
      agentId?: string | null;
      blocks: any[];
    }>;
  }>(`${BASE}/sessions/${id}/history`);
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
  return json<Array<{ name: string; description: string; trigger: string; tools: string[] }>>(url);
}

export function openSessionSocket(session_id: string): WebSocket {
  return new WebSocket(`${wsBase()}/${session_id}`);
}

// ---- workflows ----
export async function listWorkflows() {
  return json<import("./types").WorkflowDef[]>(`${BASE}/workflows`);
}
export async function listWorkflowRuns() {
  return json<import("./types").WorkflowRun[]>(`${BASE}/workflow_runs`);
}
export async function createWorkflow(data: Partial<import("./types").WorkflowDef>) {
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
  return json<{ ok: boolean; versions: Array<{ version: number; current: boolean }> }>(
    `${BASE}/workflows/${id}/versions`,
  );
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
export async function triggerWorkflowRun(workflow_id: string, context_override?: Record<string, unknown>) {
  return json<{ ok: boolean; run: import("./types").WorkflowRun }>(`${BASE}/workflow_runs`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ workflow_id, context_override }),
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
  return json<{ ok: boolean; events: Array<Record<string, unknown>> }>(
    `${BASE}/workflow_runs/${run_id}/events${qs ? `?${qs}` : ""}`,
  );
}
export async function summarizeSessionToDsl(session_id: string, provider?: string) {
  return json<
    | { ok: true; dsl: Record<string, unknown>; source_session_id: string }
    | { ok: false; error: string; raw?: string; dsl?: Record<string, unknown> }
  >(`${BASE}/workflows/summarize-from-session`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ session_id, provider }),
  });
}

// ---- artifacts ----
export async function listArtifacts(project_slug = "default") {
  return json<import("./types").Artifact[]>(`${BASE}/artifacts?project_slug=${project_slug}`);
}

// ---- settings / mcp / skills / kb ----
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
export async function getMcp() {
  return json<{ servers: string[]; tools: string[] }>(`${BASE}/mcp`);
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
export async function kbWikiBuild() {
  return json<{
    ok: boolean;
    error?: string;
    scanned?: number;
    created?: string[];
    updated?: string[];
    new_links?: unknown[];
    discovered?: unknown[];
    duration_ms?: number;
  }>(`${BASE}/kb/wiki/build`, { method: "POST" });
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

// ---- memory (P2) ----
export async function getMemory() {
  return json<{ ok: boolean; content: string; pool_count: number }>(`${BASE}/memory`);
}
export async function summarizeMemory(provider?: string) {
  return json<{ ok: boolean; summarized_chars?: number; pool_entries?: number; error?: string; message?: string }>(
    `${BASE}/memory/summarize`,
    { method: "POST", headers: H, body: JSON.stringify(provider ? { provider } : {}) },
  );
}
