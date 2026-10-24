/**
 * Connectors and the connector event stream.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, json, OVERRIDE_PORT } from "./core";

// ---- connectors (connector-module-design.md §5) ----

export type ConnectorStatus =
  | "not_installed"
  | "installing"
  | "connected"
  | "disconnected"
  | "error"
  | "disabled"
  /** mod broker 正在重启（设计 §3.3：不伪装成 installing） */
  | "restarting";

export type ConnectorKind = "builtin" | "mod";

/** 详情视图声明（mods-connector-migration-design.md §3.5）：每个连接器
 *  自己声明有哪些 tab/action；前端只共享渲染器，不共享外观。 */
export interface ConnectorDetailTab {
  id: string;
  /** overview | settings | install | activity | permissions | 自定义 */
  kind: string;
  title: string;
}

export interface ConnectorDetailAction {
  id: string;
  title: string;
}

export interface ConnectorDetailView {
  tabs?: ConnectorDetailTab[];
  actions?: ConnectorDetailAction[];
}

/** 卡片副行摘要：「N settings · M changed」。 */
export interface ConnectorConfigSummary {
  count: number;
  modified: number;
}

/** GET /api/connectors 的 runtime 段（Claude Mods 就绪度）。 */
export interface ConnectorRuntimeStatus {
  nodePath?: string | null;
  brokerOk?: boolean;
  modsEnabled?: boolean;
  loadedCount?: number;
  /** 通道状态机：disabled|connecting|connected|reconnecting|unavailable。
   *  前端据此区分「正在连」和「真的不可用」——握手期不是故障。 */
  status?: string | null;
  detail?: string;
}

/** configSchema 是 JSON Schema 子集 + x-ginno-* 扩展（设计 §3.2）。 */
export interface ConnectorSchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: Array<string | number | boolean>;
  items?: { type?: string } | Record<string, unknown>;
  "x-ginno-group"?: string;
  "x-ginno-order"?: number;
  "x-ginno-secret"?: boolean;
  "x-ginno-placeholder"?: string;
  "x-ginno-min"?: number;
  "x-ginno-max"?: number;
  [key: string]: unknown;
}

export interface ConnectorConfigSchema {
  type?: string;
  properties?: Record<string, ConnectorSchemaProperty>;
  [key: string]: unknown;
}

export interface ConnectorInfo {
  id: string;
  name: string;
  icon: string;
  description: string;
  /** builtin | mod；旧后端可能缺省，前端按 builtin 兜底。 */
  kind?: ConnectorKind;
  status: ConnectorStatus;
  statusDetail: string;
  version: string | null;
  capabilities: string[];
  enabled: boolean;
  extra?: Record<string, unknown>;
  detailView?: ConnectorDetailView;
  configSummary?: ConnectorConfigSummary;
  config?: Record<string, unknown>;
  defaults?: Record<string, unknown>;
  effective?: Record<string, unknown>;
  configSchema?: ConnectorConfigSchema;
  /** mod 的 classic hooks grant（allow | deny | ask）。 */
  classicGrant?: string;
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
  runtime?: ConnectorRuntimeStatus;
}

export async function listConnectors(): Promise<ConnectorsPayload> {
  return json<ConnectorsPayload>(`${BASE}/connectors`);
}

export async function getConnector(id: string): Promise<ConnectorInfo> {
  return json<ConnectorInfo>(`${BASE}/connectors/${encodeURIComponent(id)}`);
}

/** 详情接口：列表条目 + config/defaults/effective/configSchema/classicGrant。 */
export async function getConnectorDetail(id: string): Promise<ConnectorInfo> {
  return json<ConnectorInfo>(`${BASE}/connectors/${encodeURIComponent(id)}`);
}

export interface ConnectorApplyResult {
  ok: boolean;
  reloaded?: boolean;
  error?: string | null;
}

export interface ConnectorPatchResult {
  ok: boolean;
  config?: Record<string, unknown>;
  /** apply_config 的可等待结果（设计 §3.4）——没有它「已应用」是假的。 */
  applied?: ConnectorApplyResult;
  error?: string;
}

export async function patchConnectorConfig(
  id: string,
  config: Record<string, unknown>,
): Promise<ConnectorPatchResult> {
  return json<ConnectorPatchResult>(`${BASE}/connectors/${encodeURIComponent(id)}/config`, {
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
