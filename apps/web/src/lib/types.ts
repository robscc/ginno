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
  /** Mirrors runtime provider_is_deliberate: the provider was deliberately
   * chosen for this agent (saved via Agents settings), not the seed value. */
  provider_explicit?: boolean;
  tools_allow: string[];
  /** Connector ids this agent may NOT use (connector-module-design §8).
   *  Denylist: [] = all enabled connectors; per-agent can only restrict. */
  connectors_deny?: string[];
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
  /** The provider's model list (``models[]`` on the wire). The chat's model
   *  picker enumerates THIS — one row per entry — since a session can be pinned
   *  to any of them; ``default_model`` is only the pre-selected one.
   *
   *  Missing from this type until now, which is why the picker was written
   *  provider-by-provider against ``default_model`` alone and every extra model
   *  was unreachable from chat. */
  models?: string[];
  max_tokens?: number;
  temperature?: number;
  timeout_s?: number;
  org_id?: string;
  // Anthropic-compatible gateways that expect `Authorization: Bearer` instead of x-api-key.
  bearer_auth?: boolean;
  // Anthropic protocol only: bind the provider-side `web_search` server tool so
  // the gateway runs the search, instead of Ginno scraping a public engine.
  server_web_search?: boolean;
  server_web_search_max_uses?: number;
  // Ask OpenAI-compatible gateways (e.g. Qwen / DashScope) to use the model's
  // built-in web search (request body `enable_search: true`).
  enable_search?: boolean;
  // Hybrid-thinking models (Qwen3 commercial line etc.) default to thinking
  // OFF server-side; opt in via request body `enable_thinking: true` so the
  // endpoint streams `reasoning_content` deltas (rendered as thinking blocks).
  enable_thinking?: boolean;
}

export type Providers = Record<string, ProviderConfig>;

// ---- multi-provider model configs (multi-provider-model-config.md §3.1) ----
// New array-shaped settings key `model_configs`; protocol enum is kebab-case.
// The three built-in legacy ids (anthropic/openai/custom) survive as ordinary
// config ids so existing agent/session/usage references stay valid.
export type ModelProtocol = "anthropic" | "openai-compatible" | "openai-responses";

export interface ModelConfig {
  id: string; // stable key ("anthropic"/"openai"/"custom" for migrated slots, "prov_*" for new)
  name: string; // user-readable, required
  protocol: ModelProtocol;
  base_url: string;
  api_key: string;
  org_id?: string; // openai-responses only
  bearer_auth?: boolean; // anthropic only: Authorization: Bearer instead of x-api-key
  server_web_search?: boolean; // anthropic only: gateway-side web_search tool
  server_web_search_max_uses?: number;
  models: string[]; // replaces the old single model/default_model field
  default_model: string; // must be a member of models
  max_tokens: number;
  context_window?: number; // model context window (tokens); 0/absent = unknown
  temperature: number;
  timeout_s: number;
  enable_search?: boolean; // openai-compatible private body param (Qwen/DashScope…)
  enable_thinking?: boolean; // openai-compatible hybrid-thinking opt-in
  enabled: boolean;
  // Unix seconds of the last successful verify (persisted server-side);
  // null/absent = never verified. last_error holds the most recent failure.
  verified_at?: number | null;
  last_error?: string | null;
}

// Refusal detail returned by PUT/verify when the target config is the global
// default or still referenced (Q8). Values are counts or id lists depending
// on the backend — render defensively (see describeRefs).
export interface ModelConfigRefs {
  agents?: number | string[];
  sessions?: number | string[];
  workflows?: number | string[];
  [k: string]: unknown;
}

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
  // Context folder mounts (docs/context-folders-design.md): library ids +
  // the one mount whose dir is the bash cwd / relative-path base.
  context_folders?: string[];
  primary_folder?: string | null;
  // Bound workflow for workflow-dev refine sessions (injected every turn).
  workflow_id?: string | null;
  // 侧栏置顶（sidebar pin）：显式置顶的会话排在天分组之上。
  pinned?: boolean;
  // "quick" = created by the floating quick-chat window
  // (docs/floating-window-design.md §1.1); regular sessions omit the field.
  // "subagent" = a spawned child session (subagent-design.md §4.1); the parent
  // linkage lives in parent_session_id/depth/subagent below.
  // "scheduled" = 定时任务影子会话（scheduled-tasks-design.md §4.3）；列表与
  // 搜索均整类隐藏（§10 决议 5），只能从 /scheduled 的执行记录/时间条进入回放。
  type?: "quick" | "subagent" | "scheduled" | "delegation";
  /** delegation 专用：running | success | error | timeout（bg 终态/启动对账写入） */
  stop_reason?: string;
  backend?: string;
  mode?: string;
  // 影子会话所属的执行记录 run_id（§4.3）；普通会话缺省。
  schedule_run_id?: string;
  // ---- subagent (subagent-design.md §4.1；与共享契约 1 同形) ----
  // Parent session (main conversation or an upper-layer subagent).
  parent_session_id?: string;
  // 0/1/2 — main conversations and plain sessions omit the field.
  depth?: number;
  subagent?: SubagentMeta;
}

export type SubagentStatus = "running" | "waiting" | "done" | "failed" | "stopped";

export interface SubagentMeta {
  goal: string;
  constraints: string;
  acceptance: string;
  origin: "user" | "agent";
  status: SubagentStatus;
  result_summary: string;
  // P3 共享契约 2：spawn 模式。"standard" = 全新上下文（默认）；"fork" = 初始
  // 历史为父对话 checkpoint 副本。P1/P2 存量无此字段，视为 "standard"。
  mode?: "standard" | "fork";
  // P3 契约 1：命中的子代理类型名（空 = 默认 persona）。UI 用它替代继承来的
  // persona 名（子会话的 persona 永远是父会话那个，标注没有信息量）。
  agent_type?: string;
}

// WS 帧 subagent.spawned（契约 2）——广播到父 session 与子 session 的所有 socket。
export interface SubagentSpawnEvent {
  session_id: string;
  parent_session_id: string;
  goal: string;
  constraints?: string;
  acceptance?: string;
  depth?: number;
  origin?: "user" | "agent";
  title?: string;
  /** 命中的子代理类型名（~/.ginno/agents/subagents/*.md），空 = 默认 persona */
  agent_type?: string;
  /** 会话类型透传:"delegation" = 委托子会话(store 按 delegation 行渲染,不加发起卡) */
  type?: string;
  /** delegation 专用:外部后端名(claude/pi/codex) */
  backend?: string;
  /** 占位行图标(delegation 传 terminal;缺省 boxes) */
  icon?: string;
}

// WS 帧 subagent.status（契约 2）。
export interface SubagentStatusEvent {
  session_id: string;
  parent_session_id: string;
  status: SubagentStatus;
  result_summary?: string;
  error?: string;
}

// ---- 拆分方案（P2 共享契约 1/2，subagent-design.md §5.1/§5.2）----------------
// WS 下行 subagent.plan：LLM 拆分结果，广播到当前 session 的 socket。用户在
// 拆分方案卡片里确认/编辑后才批量 spawn（防泛滥闸门，设计 §5.2）。
export interface SubagentPlanSubtask {
  goal: string;
  constraints?: string;
  acceptance?: string;
  // 委派理由（附录 A.7）：用户快速判断拆分是否合理的主要审阅物。
  reason?: string;
}

export interface SubagentPlanEvent {
  plan_id: string;
  session_id: string;
  task: string;
  subtasks: SubagentPlanSubtask[];
}

// WS 下行 subagent.plan.cancelled（P3 共享契约 5）：另一个窗口（或 runtime 侧
// 主动）取消了一个还在 pending 的拆分方案——本窗口把对应卡片翻成已取消态。
export interface SubagentPlanCancelledEvent {
  plan_id: string;
  session_id?: string;
}

// A registered context folder (~/.ginno/folders.json entry).
export interface FolderEntry {
  id: string;
  path: string;
  name: string;
  access: "ro" | "rw";
  load_rules: boolean;
  added?: string;
  last_used?: string;
}

// Resolved mount as seen by a session (context_dirs entry).
export interface ContextDirEntry {
  id: string;
  path?: string;
  name?: string;
  access?: "ro" | "rw";
  load_rules?: boolean;
  missing?: boolean;
}

export interface FolderProbe {
  ok: boolean;
  error?: string;
  path?: string;
  is_dir?: boolean;
  file_count?: number;
  file_count_truncated?: boolean;
  has_git?: boolean;
  rule_file?: string | null;
  already_registered?: boolean;
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
  // Count of soft-failed steps (on_error="continue", stability plan P2): the
  // run still ends "done" but with degraded output — UI shows a ⚠ badge.
  warnings?: number;
  context_override?: Record<string, unknown> | null; // inputs this run executed with
  retried_from?: string | null; // run id this one re-executes
  retry_run_id?: string | null; // set on the original once it has been retried
  session_id?: string | null;
  present_in_session_id?: string | null;
  // 触发来源（scheduled-tasks-design §5.3）：调度器创建的 run 打
  // "schedule"，Workflows 运行列表据此显示「⏰ 定时」徽标；手动/会话内运行缺省。
  origin?: string | null;
  // Why the run is paused (workflow-ux-redesign P1): stamped by the server when
  // the run transitions to "paused". kind "human" → show the question card;
  // "manual" → user pause (#14), generic 继续/取消 controls.
  pending_interrupt?: {
    kind?: string; // "human" | "manual" | "supervisor"
    node_id?: string | null;
    question?: string | null;
    // Present when auto-mode escalation parked the run at a supervisor gate
    // (design B P2.5): why the adjudicator handed off to a human, plus what it
    // would have done on its own.
    fallback_reason?: string; // "low-confidence" | "interventions-exceeded" | "token-budget" | "retry-limit" | "judge-error"
    auto_suggestion?: string | { decision?: string; confidence?: number; reason?: string } | null;
    [k: string]: unknown;
  } | null;
}

/** One execution event from ``runs/<id>.events.jsonl`` (GET /workflow_runs/{id}/events).
 *  Kept open-ended ([k: string]: unknown) so existing `Record<string, unknown>`
 *  consumers keep compiling while new fields ride along. */
export interface WorkflowRunEvent {
  ts?: number;
  run_id?: string;
  seq?: number; // monotonic per run (design B P2): WS dedup after a reconnect
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
  kind: string; // spreadsheet | table | document | presentation | pdf | data | text | image
  mime?: string;
  size?: number;
  session_id?: string;
  artifact_id?: string | null;
  stale?: boolean;
  mtime?: number;
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
  type?: string | null;
  confidence?: string | null;
}

// ---- memory refinery (draft → review → apply) ----
export interface MemoryDraft {
  draft: string | null;
  previous?: string;
  diff?: string;
  pool_entries?: number;
  pool_cutoff?: number | null;
  created_at?: number;
  trigger?: string;
  budget?: number;
  chars?: number;
  over_budget?: boolean;
}

export interface PromoteSimilar {
  title: string;
  path: string;
  score: number;
}

export interface PromotePreview {
  ok: boolean;
  error?: string;
  suggestion?: "create" | "merge";
  merge_target?: PromoteSimilar | null;
  similar?: PromoteSimilar[];
  draft?: { path: string; raw: string; title: string };
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
  /** Per-model SKU rows for the day (bar-hover breakdown), tokens desc. */
  models?: UsageModelAgg[];
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
/** 一个 2 小时桶的用量：[gross, net, out, cache, requests] */
export type UsageGridCell = [gross: number, net: number, out: number, cache: number, requests: number];

/** 连续日历的 2 小时点格矩阵。grid[i] 对应 days[i]，每天固定 12 个桶（本地时间 00:00–24:00）。
 *  没有 jsonl 文件的日子也占一列，整行全 0 —— 前端不区分「无文件」与「零请求」。 */
export interface UsageGrid {
  ok: boolean;
  days: string[];
  grid: UsageGridCell[][];
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

// ---- 定时任务（docs/scheduled-tasks-design.md §4；字段名是跨 agent 契约）----
export type ScheduleTargetType = "prompt" | "workflow";

/** prompt 目标：每次触发新建影子会话跑一轮（§4.1）。 */
export interface ScheduleTargetPrompt {
  type: "prompt";
  prompt: string;
  agent_id?: string | null;
  project_slug?: string | null;
}

/** workflow 目标：headless run，输入落 context_override（§4.1）。 */
export interface ScheduleTargetWorkflow {
  type: "workflow";
  workflow_id: string;
  context_override?: Record<string, unknown> | null;
}

export type ScheduleTarget = ScheduleTargetPrompt | ScheduleTargetWorkflow;

/** 四种预置计划（§4.1）。weekly.weekday 0=周日；once.at 为本地时间字符串。 */
export type SchedulePlan =
  | { kind: "interval"; minutes: number }
  | { kind: "daily"; at: string }
  | { kind: "weekly"; weekday: number; at: string }
  | { kind: "once"; at: string };

/** 冗余展示字段：最近一次执行的摘要（§4.1 last_run）。 */
export interface ScheduleLastRun {
  run_id: string;
  status: string;
  started?: number | null;
  finished?: number | null;
}

export interface ScheduleTask {
  id: string;
  name: string;
  target: ScheduleTarget;
  schedule: SchedulePlan;
  enabled: boolean;
  notify?: boolean; // P1
  created: number;
  updated: number;
  /** 调度器回写（epoch 秒）；UI 冷启动即有值。null/缺省 = 暂停或单次已完成。 */
  next_run_at?: number | null;
  last_run?: ScheduleLastRun | null;
}

/** GET /api/schedule → 全局配置 + 任务列表。keep_awake 是唯一真值（§3.3）。 */
export interface ScheduleConfig {
  enabled: boolean;
  keep_awake: boolean;
  tasks: ScheduleTask[];
}

/** 执行记录行（§4.2 jsonl，同 run_id 末行胜出；API 已去重）。 */
export interface ScheduleRun {
  run_id: string;
  task_id: string;
  task_name: string;
  target_type: ScheduleTargetType;
  trigger: "schedule" | "manual" | string;
  status: "running" | "ok" | "error" | "skipped_overlap" | "missed" | string;
  scheduled_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  /** prompt 目标：影子会话 id；workflow 目标：null。 */
  session_id: string | null;
  /** workflow 目标；prompt 为 null。 */
  workflow_id: string | null;
  workflow_run_id: string | null;
  summary: string | null;
  error: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

/** GET /api/schedule/runs → 分页行（store.query_runs 的返回形状：``rows``）。
 *  timeline 接口的字段才是 ``runs``，两处不同名——字段名以 runtime 实现为准。 */
export interface ScheduleRunsPage {
  ok?: boolean;
  date?: string;
  rows: ScheduleRun[];
  total?: number;
  page?: number;
  page_size?: number;
}

/** GET /api/schedule/timeline → 已执行 + 当天剩余计划点（§5.1）。 */
export interface ScheduleTimeline {
  ok?: boolean;
  runs: ScheduleRun[];
  planned: Array<{ task_id: string; task_name: string; at: number }>;
}

/** WS /api/ws/schedule 下行事件（§5.1）。载荷开放；收到即重拉数据。 */
export interface ScheduleWsEvent {
  type: "run_started" | "run_finished" | "task_updated" | "missed" | string;
  [k: string]: unknown;
}
