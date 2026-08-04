export type Priority = "high" | "medium" | "low";

export interface Todo {
  id: string;
  title: string;
  priority: Priority;
  category: string;
  due: string;
  done: boolean;
  links: { session_id?: string; workflow_id?: string };
  created: number;
  completed_at: number | null;
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
