export type Priority = "high" | "medium" | "low";

// Long-running per-session goal (goal-design.md). One goal per session.
export type GoalStatus = "active" | "paused" | "blocked" | "usage_limited" | "complete";

export interface Goal {
  goal_id: string;
  objective: string;
  status: GoalStatus;
  time_used_seconds: number;
  turns_used: number;
  agent_id?: string | null;
  created_at: number;
  updated_at: number;
  /** Live hint from the sidecar when a Space is waiting on the human. */
  browser_state?: "waiting_human" | string | null;
}

export interface Todo {
  id: string;
  title: string;
  priority: Priority;
  category: string;
  due: string;
  done: boolean;
  emoji?: string; // optional icon rendered before the title
  tags?: string[]; // free-form labels
  session_ids?: string[]; // sessions where the item was mentioned/worked on
  artifact_ids?: string[]; // deliverables linked to the item
  links: { session_id?: string; workflow_id?: string };
  // Loose external refs — one entry per attached TODO platform. Unknown keys
  // preserved; see runtime todo-providers for the provider registry.
  ext?: TodoExtRef[];
  created: number;
  completed_at: number | null;
}

export interface TodoExtRef {
  provider?: string;
  id?: string;
  url?: string;
  title?: string;
  due?: string;
  [k: string]: unknown;
}

export interface TodoProvider {
  id: string;
  label: string;
  skill?: string | null;
  mcp?: string | null;
  auto_push?: boolean;
  source?: string;
}

export interface TodoSyncEntry {
  todo_id: string;
  provider: string;
  ext_id: string;
  direction: string;
  run_id: string;
  status: "running" | "ok" | "failed" | string;
  error?: string;
  at: number;
}

export interface AgentConfig {
  id: string;
  name: string;
  icon: string;
  color: string;
  system_prompt: string;
  provider: string;
  model: string;
  tools_allow: string[];
  memory_scope: string;
  status: string;
}

export interface ProviderConfig {
  enabled: boolean;
  protocol: "anthropic" | "openai" | "openai-compatible" | string;
  api_key: string;
  base_url: string;
  default_model?: string;
  model?: string;
  name?: string;
  max_tokens?: number;
  temperature?: number;
  timeout_s?: number;
  org_id?: string;
  // Anthropic-compatible gateways that expect `Authorization: Bearer` instead of x-api-key.
  bearer_auth?: boolean;
  // Ask OpenAI-compatible gateways (e.g. Qwen / DashScope) to use the model's
  // built-in web search (request body `enable_search: true`).
  enable_search?: boolean;
}

export type Providers = Record<string, ProviderConfig>;

export interface SessionMeta {
  id: string;
  title: string;
  title_auto?: boolean;
  icon: string;
  agent_id: string | null;
  provider: string;
  model: string;
  created: number;
  updated: number;
}

export interface VerifyResult {
  ok: boolean;
  error?: string;
  latency_ms?: number;
}

// Cumulative model usage for one session, pushed by the runtime `usage` WS
// event (docs/design/world-state-plan.md D2/D4). Tokens are provider-reported;
// cache_read is the prompt-cache hit portion billed at cache rates.
export interface SessionUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  calls: number;
}

// One WorldState change announced via the `context.updated` WS event (C3).
export interface ContextChange {
  section: string; // environment | permissions | agent | skills | memory | mcp
  summary: string;
}

export interface WorkflowStep {
  id: string;
  title: string;
  status: string;
  output?: string;
  agent_id?: string | null;
}

export interface WorkflowDef {
  id: string;
  name: string;
  description: string;
  steps: WorkflowStep[];
  version?: number; // current DSL version (P1+)
  dsl?: Record<string, unknown>; // compiled DSL (P1+); absent on legacy payloads
  system?: boolean; // built-in seed: listed but not deletable
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  name: string;
  status: string;
  steps: WorkflowStep[];
  started: number;
  updated: number;
  dsl_version?: number; // DSL version this run executed (P2+)
  error?: string | null; // last failure reason (failed/interrupted/cancelled)
  // Structured companion of `error` for localization: which node failed + the
  // trimmed traceback. Optional on legacy runs (created before this existed).
  error_detail?: { node_id?: string | null; traceback?: string | null } | null;
  finished?: number | null; // wall-clock end for terminal runs
  context_override?: Record<string, unknown> | null; // inputs this run executed with
  retried_from?: string | null; // run id this one re-executes
  retry_run_id?: string | null; // set on the original once it has been retried
  session_id?: string | null;
  present_in_session_id?: string | null;
  // Why the run is paused (workflow-ux-redesign P1): stamped by the server when
  // the run transitions to "paused". kind "human" → show the question card;
  // "manual" → user pause (#14), generic 继续/取消 controls.
  pending_interrupt?: {
    kind?: string; // "human" | "manual" | "browser_handoff"
    node_id?: string | null;
    question?: string | null;
    space?: string;
    url?: string;
    reason?: string;
    [k: string]: unknown;
  } | null;
}

export type BrowserOwner = "agent" | "agentDelegatedToUser" | "user";

export interface BrowserSpace {
  name: string;
  owner: BrowserOwner;
  url?: string;
  title?: string;
  bound_session_id?: string | null;
  bound_run_id?: string | null;
  reason?: string;
  keep?: boolean;
  headed?: boolean;
  pending_risky_url?: string;
  tabs?: string[];
}

export interface BrowserTab {
  id: string;
  url?: string;
  title?: string;
  active?: boolean;
}

export interface BrowserDownload {
  id: string;
  space?: string;
  url?: string;
  filename?: string;
  path?: string;
  state?: string;
  bytes?: number;
  ts?: number;
}

export interface BrowserState {
  ok: boolean;
  active_space?: string | null;
  url?: string;
  focus?: string | null;
  spaces: BrowserSpace[];
  waiting_human?: boolean;
  headed?: boolean;
  engine?: "idle" | "fake" | "chrome" | "cef" | string;
  engine_error?: string | null;
  error?: string;
}

export interface ChromeImportStatus {
  ok?: boolean;
  chrome_user_data?: string;
  chrome_running?: boolean;
  profiles?: Array<{ id: string; path?: string; has_cookies?: boolean; label?: string }>;
  ginno_profile?: string;
  imported?: boolean;
  imported_from?: string;
  error?: string;
}

/** One execution event from ``runs/<id>.events.jsonl`` (GET /workflow_runs/{id}/events).
 *  Kept open-ended ([k: string]: unknown) so existing `Record<string, unknown>`
 *  consumers keep compiling while new fields ride along. */
export interface WorkflowRunEvent {
  ts?: number;
  run_id?: string;
  kind?: string; // node_enter | node_exit | tool_call | tool_result | context_write | loop_iter | loop_skip | loop_cap | interrupt | resume | error | done | paused | cancelled | interrupted
  node_id?: string | null;
  node_type?: string;
  status?: string;
  error?: string;
  traceback?: string; // present on error events (trimmed tail)
  name?: string; // tool_result: tool name
  content?: string; // tool_result: output (server caps at 2000 chars)
  calls?: Array<{ name?: string; args?: unknown }>; // tool_call
  keys?: string[]; // context_write
  method?: string; // context_write: "write_json" | "llm" (master-plan §2.2)
  usage?: { input_tokens?: number; output_tokens?: number }; // node_exit telemetry
  index?: number; // loop_iter
  of?: number; // loop_iter
  question?: string; // interrupt
  [k: string]: unknown;
}

export interface Artifact {
  id: string;
  kind: string;
  name: string;
  ref: string;
  session_id?: string | null;
  created: number;
  schema?: string; // user-corrected schema summary (prompt-injection override)
}

// Metadata inspector payload for one artifact (GET /api/artifacts/{id}/metadata).
export interface ArtifactMeta {
  ok: boolean;
  error?: string;
  artifact?: Artifact;
  file?: FileEntry | null;
  exists?: boolean;
  schema?: string;
  schema_source?: "override" | "computed" | "";
}

// Inspector edits (PUT /api/artifacts/{id}). schema = injection override;
// file_kind = registry classification correction.
export interface ArtifactPatch {
  name?: string;
  kind?: string;
  schema?: string;
  file_kind?: string;
}

// ---- files (upload / preview) ----
// Summary row returned by GET /api/skills — feeds the composer's / command menu.
export interface SkillSummary {
  name: string;
  description: string;
  trigger: string; // user-invocable | model-invocable | both
  tools: string[];
  builtin?: boolean; // shipped with Ginno; cannot be deleted
}

export interface FileEntry {
  id: string;
  name: string;
  path: string;
  kind: string; // spreadsheet | table | document | presentation | pdf | data | text
  mime?: string;
  size?: number;
  session_id?: string;
  artifact_id?: string | null;
  stale?: boolean;
}

// Settings → 会话文件: one row per per-session files directory.
export interface SessionDirSummary {
  project_slug: string;
  session_id: string;
  title?: string | null; // null → session deleted (dir preserved, orphaned)
  orphaned: boolean;
  dir: string;
  file_count: number;
  total_bytes: number;
  mtime: number;
}

export interface SessionDirEntry {
  name: string;
  type: "file" | "dir";
  size: number;
  mtime: number;
}

export interface FilePreviewSheet {
  name: string;
  rows: number;
  cols: number;
}

export interface FilePreviewColumn {
  name: string;
  dtype: string;
}

// Tables → paginated grid; documents → markdown. Discriminated by `kind`.
export interface FilePreview {
  ok: boolean;
  error?: string;
  file?: FileEntry;
  kind: string;
  // table kinds:
  sheets?: FilePreviewSheet[];
  sheet?: string;
  columns?: FilePreviewColumn[];
  rows?: string[][];
  total_rows?: number;
  offset?: number;
  limit?: number;
  // document kinds:
  markdown?: string;
  metadata?: Record<string, unknown>;
}

// ---- knowledge base / LLMWiki ----
export interface WikiSearchResult {
  title: string;
  path: string;
  tags: string[];
  summary: string;
  score: number;
  matched_terms: string[];
}

export interface WikiPage {
  title: string;
  path: string;
  tags: string[];
  links: string[];
  modified: number;
}

export interface WikiPageDoc {
  ok: boolean;
  exists?: boolean;
  error?: string;
  path: string;
  title: string;
  tags: string[];
  links: string[];
  raw: string;
}

export interface WikiStats {
  ok: boolean;
  error?: string;
  vault_path?: string;
  total_pages?: number;
  pages_by_dir?: Record<string, number>;
  total_links?: number;
  total_tags?: number;
  unique_tags?: string[];
  last_indexed?: number;
}

export interface WikiAssocPair {
  a: string;
  b: string;
  score: number;
  type: string;
}
export interface WikiCluster {
  label: string;
  members: string[];
  density: number;
}
export interface WikiRelatedItem {
  title: string;
  score: number;
  type: string;
}
export interface WikiDiscover {
  ok: boolean;
  strong: WikiAssocPair[];
  clusters: WikiCluster[];
  isolated: string[];
  orphan_bridges: WikiAssocPair[];
  merge_candidates: { a: string; b: string; score: number }[];
  stats: { pages: number; edges: number };
}

// ---- usage telemetry (usage-stats-design.md) ----
// Canonical token counters: input_tokens is the WHOLE prompt (cache portions
// included), so cache_hit_ratio = cache_read / input is always in [0, 1].
export interface UsageCounters {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  calls: number;
  cache_hit_ratio: number;
}
export interface UsageDailyPoint extends UsageCounters {
  date: string; // YYYY-MM-DD
}
export interface UsageProviderAgg extends UsageCounters {
  provider: string;
}
export interface UsageModelAgg extends UsageCounters {
  provider: string;
  model: string;
}
export interface UsageSourceAgg extends UsageCounters {
  source: string; // usage-stats-design §3.6: chat / goal / workflow / …
}
export interface UsageOverview {
  ok: boolean;
  window: { days: number; from: string; to: string };
  today: UsageCounters;
  totals: UsageCounters;
  sessions_active: number;
  daily: UsageDailyPoint[];
  providers: UsageProviderAgg[];
  models: UsageModelAgg[];
  sources?: UsageSourceAgg[];
}
export interface UsageHourPoint {
  hour: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  calls: number;
}
export interface UsageHourly {
  ok: boolean;
  date: string;
  hours: UsageHourPoint[];
}
export interface UsageSessionRow extends UsageCounters {
  session_id: string;
  project_slug: string | null;
  agent_id: string | null;
  last_active: number;
  title: string;
  icon: string;
  provider: string;
  model: string;
  deleted: boolean;
}
export interface UsageSessions {
  ok: boolean;
  sessions: UsageSessionRow[];
}
export interface UsageRequest {
  ts: number;
  session_id: string | null;
  project_slug: string | null;
  agent_id: string | null;
  turn_id: string | null;
  source: string;
  provider: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  latency_ms: number | null;
  ok: boolean;
  error: string | null;
}
export interface UsageRequests {
  ok: boolean;
  date: string;
  total: number;
  page: number;
  page_size: number;
  rows: UsageRequest[];
}
