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
}

export type Providers = Record<string, ProviderConfig>;

export interface SessionMeta {
  id: string;
  title: string;
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
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  name: string;
  status: string;
  steps: WorkflowStep[];
  started: number;
  updated: number;
}

export interface Artifact {
  id: string;
  kind: string;
  name: string;
  ref: string;
  session_id?: string | null;
  created: number;
}
