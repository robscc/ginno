/**
 * Context folders, artifacts, files (upload / preview), todo
 * providers, settings / MCP / skills and mods.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json, jsonStrict } from "./core";
// 非 hook 层取已解析 locale（模块级镜像，provider 渲染期同步赋值）：随每个
// API 请求/WS 连接下发给 runtime（i18n-design.md §1/§5）。
import { currentLocale } from "../../i18n/provider";
import type { Todo } from "../types";

// ---- context folders (docs/context-folders-design.md) ----
export async function listFolders() {
  return json<{
    ok: boolean;
    folders: import("../types").FolderEntry[];
    /** 解析后的运行时数据 home（特权挂载预设目标；$GINNO_HOME 可重定向） */
    runtime_home?: string;
  }>(`${BASE}/folders`);
}

export async function createFolder(data: {
  path: string;
  name?: string;
  access?: "ro" | "rw";
  load_rules?: boolean;
  auto_mount?: boolean;
}) {
  return json<{
    ok: boolean;
    error?: string;
    folder?: import("../types").FolderEntry;
    probe?: import("../types").FolderProbe;
  }>(`${BASE}/folders`, { method: "POST", headers: H, body: JSON.stringify(data) });
}

export async function probeFolder(path: string) {
  return json<import("../types").FolderProbe>(`${BASE}/folders/probe`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ path }),
  });
}

export async function updateFolder(id: string, patch: Partial<import("../types").FolderEntry>) {
  return json<{ ok: boolean; error?: string; folder?: import("../types").FolderEntry }>(
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
    context_dirs?: import("../types").ContextDirEntry[];
    primary_path?: string | null;
  }>(`${BASE}/sessions/${id}/context`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(body),
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

export async function listSkills(project_slug?: string) {
  const url = new URL(`${BASE}/skills`);
  if (project_slug) url.searchParams.set("project_slug", project_slug);
  return json<import("../types").SkillSummary[]>(url);
}
// ---- artifacts ----
// Artifacts belong to a session: pass session_id to scope the list (the
// Artifacts panel does this); omit for all artifacts (back-compat).
export async function listArtifacts(project_slug = "default", session_id?: string) {
  const q = session_id ? `&session_id=${encodeURIComponent(session_id)}` : "";
  return json<import("../types").Artifact[]>(
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
export async function getArtifactMetadata(
  id: string, project_slug = "default", options?: { candidates?: boolean; includeSchema?: boolean; strict?: boolean },
) {
  const candidates = options?.candidates ? "&candidates=true" : "";
  const schema = options?.includeSchema === false ? "&include_schema=false" : "";
  return (options?.strict ? jsonStrict : json)<import("../types").ArtifactMeta>(
    `${BASE}/artifacts/${id}/metadata?project_slug=${project_slug}${candidates}${schema}`,
  );
}

// User corrections from the inspector. schema → injection override;
// file_kind → registry classification fix.
export async function updateArtifact(
  id: string,
  patch: import("../types").ArtifactPatch,
  project_slug = "default",
) {
  return json<{ ok: boolean; error?: string; artifact?: import("../types").Artifact }>(
    `${BASE}/artifacts/${id}?project_slug=${project_slug}`,
    { method: "PUT", headers: H, body: JSON.stringify(patch) },
  );
}

// ---- files (upload / preview — docs/file-parsing-research.md §7) ----
/** 上传/挂载响应里的稳定错误码。`file_too_large` = 超过 runtime 的
 *  UPLOAD_MAX_BYTES（100MB）——前端据此给出专门的可见报错，而不是把芯片
 *  静默移除（2026-11-22 R3 巡检 Q1）。attach-path 超限时响应还带实际
 *  文件 `size`（字节），供错误文案呈现（Q2）。 */
export type FileOpErrorCode = "file_too_large";

export async function uploadFile(sessionId: string, file: File) {
  const fd = new FormData();
  fd.append("session_id", sessionId);
  fd.append("file", file);
  // NOTE: no Content-Type header — the browser sets the multipart boundary.
  return json<{ ok: boolean; error?: string; code?: FileOpErrorCode; file?: import("../types").FileEntry }>(
    `${BASE}/files`,
    { method: "POST", body: fd },
  );
}

const _filesInflight = new Map<string, Promise<import("../types").FileEntry[]>>();

export async function listFiles(project_slug = "default", session_id?: string) {
  const q = session_id ? `&session_id=${encodeURIComponent(session_id)}` : "";
  // In-flight dedupe (2026-11-16 perf): a session switch fired 16 concurrent
  // identical /files requests (file picker + @ menu + context panel all load
  // on mount). Concurrent callers share one promise; it clears on settle so
  // the next call still sees fresh data.
  const key = `${project_slug}|${session_id ?? ""}`;
  const existing = _filesInflight.get(key);
  if (existing) return existing;
  const p = json<import("../types").FileEntry[]>(
    `${BASE}/files?project_slug=${project_slug}${q}`,
  ).finally(() => {
    _filesInflight.delete(key);
  });
  _filesInflight.set(key, p);
  return p;
}

// Attach an OS file by native path (Tauri desktop drag & drop — WKWebView can't
// expose dropped files to JS, so the shell forwards the path and the sidecar
// copies + registers it). Returns the same shape as uploadFile.
export async function attachFilePath(sessionId: string, path: string) {
  return json<{
    ok: boolean;
    error?: string;
    code?: FileOpErrorCode;
    /** 超限拒绝时的实际文件大小（字节），attach-path 场景前端拿不到
     *  本地 File 对象，文案里的 size 由后端提供。 */
    size?: number;
    file?: import("../types").FileEntry;
  }>(`${BASE}/files/attach-path`, { method: "POST", headers: H, body: JSON.stringify({ session_id: sessionId, path }) });
}

// Register an existing absolute path (an attach_ref ref_id) in the file
// registry — no copy, idempotent by path. Live turns register on ref.emit,
// but refs replayed from old sessions never were; the chip's click calls this
// to obtain a preview-able file id. Same shape as uploadFile/attachFilePath.
export async function ensureFileRegistered(sessionId: string, path: string, name?: string) {
  return json<{
    ok: boolean;
    error?: string;
    code?: "file_not_found" | "session_not_found";
    file?: import("../types").FileEntry;
  }>(
    `${BASE}/files/ensure-registered`,
    {
      method: "POST",
      headers: H,
      body: JSON.stringify({ session_id: sessionId, path, ...(name ? { name } : {}) }),
    },
  );
}


export async function getFilePreview(
  fileId: string,
  opts: { sheet?: string; offset?: number; limit?: number } = {},
) {
  const p = new URLSearchParams();
  if (opts.sheet) p.set("sheet", opts.sheet);
  p.set("offset", String(opts.offset ?? 0));
  p.set("limit", String(opts.limit ?? 100));
  return json<import("../types").FilePreview>(`${BASE}/files/${fileId}/preview?${p.toString()}`);
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

// Sandbox-rendered html preview for the FileViewer iframe (mirrors the code
// panel's HtmlPreview): CSP header server-side + sandbox attr client-side.
export function fileInlineUrl(fileId: string) {
  return `${BASE}/files/${fileId}/inline`;
}

type FileActionReason = "fileUnregistered" | "fileMoved" | "launcherFailed" |
  "fileManagerFailed" | "downloadsWriteFailed" | "httpFailed" | "requestFailed" |
  "invalidResponse" | "connectionFailed" | "businessRejected";

export type FileActionFailure = {
  ok: false;
  error?: string;
  reason: FileActionReason;
  status?: number;
};

type FileResponse = { ok?: boolean; error?: unknown; detail?: unknown; path?: string; name?: string };

function fileResponse(value: unknown): FileResponse | undefined {
  return value !== null && typeof value === "object" ? value as FileResponse : undefined;
}

function fileActionFailure(fileId: string, status: number, body?: FileResponse): FileActionFailure {
  const detail = fileResponse(body?.detail);
  const rejected = body?.ok === false || detail?.ok === false;
  const error = [detail?.error, body?.error, body?.detail]
    .find((value): value is string => typeof value === "string" && value.trim() !== "");
  // 只识别文件端点的已知业务响应。普通 HTTP 404 或解析器里提到的路径均不代表文件丢失。
  if (rejected && (status === 404 || status < 300)) {
    if (error === `file not found: ${fileId}`) return { ok: false, reason: "fileUnregistered" };
    if (error === "File has been moved or deleted") return { ok: false, reason: "fileMoved" };
  }
  if (rejected && status < 300) {
    if (error?.startsWith("Failed to launch external app: ")) {
      return { ok: false, reason: "launcherFailed", error: error.slice("Failed to launch external app: ".length) };
    }
    if (error?.startsWith("Failed to open file manager: ")) {
      return { ok: false, reason: "fileManagerFailed", error: error.slice("Failed to open file manager: ".length) };
    }
    if (error?.startsWith("Failed to write to Downloads: ")) {
      return { ok: false, reason: "downloadsWriteFailed", error: error.slice("Failed to write to Downloads: ".length) };
    }
  }
  if (status >= 400) {
    return { ok: false, reason: "httpFailed", status, error };
  }
  return error ? { ok: false, reason: "businessRejected", error } : { ok: false, reason: "invalidResponse" };
}

async function readFileResponse(res: Response) {
  try {
    return fileResponse(await res.json());
  } catch {
    return undefined;
  }
}

const fileRequestHeaders = () => ({ "X-Ginno-Language": currentLocale() });

type FileDownloadOptions = { fmt?: "raw" | "csv"; sheet?: string };

async function fetchFileDownload(
  fileId: string, opts?: FileDownloadOptions, signal?: AbortSignal,
): Promise<{ ok: true; response: Response } | FileActionFailure> {
  try {
    const res = await fetch(fileDownloadUrl(fileId, opts), {
      headers: fileRequestHeaders(), signal, cache: "no-store",
    });
    if (!res.ok) return fileActionFailure(fileId, res.status, await readFileResponse(res));
    // Raw JSON files also use application/json, with an attachment header.
    // Older runtimes may return a bare successful-HTTP business rejection.
    if (!res.headers.get("content-disposition") && res.headers.get("content-type")?.includes("application/json")) {
      const body = await readFileResponse(res.clone());
      if (body?.ok === false || fileResponse(body?.detail)?.ok === false) {
        return fileActionFailure(fileId, res.status, body);
      }
    }
    return { ok: true, response: res };
  } catch {
    return { ok: false, reason: "connectionFailed" };
  }
}

export async function readImageFile(
  fileId: string, signal: AbortSignal,
): Promise<{ ok: true; blob: Blob } | FileActionFailure> {
  const result = await fetchFileDownload(fileId, undefined, signal);
  if (!result.ok) return result;
  try {
    return { ok: true, blob: await result.response.blob() };
  } catch {
    return { ok: false, reason: "requestFailed" };
  }
}

// Browser-side save: fetch as blob → object URL → anchor click. The desktop
// webview uses saveFileToDownloads because WKWebView cannot trigger this download.
export async function downloadFile(
  fileId: string,
  fallbackName: string,
  opts: FileDownloadOptions = {},
): Promise<{ ok: true } | FileActionFailure> {
  const result = await fetchFileDownload(fileId, opts);
  if (!result.ok) return result;
  try {
    const res = result.response;
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
    return { ok: false, reason: "requestFailed" };
  }
}

async function postFileAction(fileId: string, action: string, opts?: FileDownloadOptions):
  Promise<{ ok: true; path?: string; name?: string } | FileActionFailure> {
  try {
    const res = await fetch(`${BASE}/files/${fileId}/${action}`, {
      method: "POST", headers: { ...H, ...fileRequestHeaders() },
      ...(opts ? { body: JSON.stringify(opts) } : {}),
    });
    const body = await readFileResponse(res);
    if (!res.ok || body?.ok !== true) return fileActionFailure(fileId, res.status, body);
    return { ...body, ok: true };
  } catch {
    return { ok: false, reason: "connectionFailed" };
  }
}

export function saveFileToDownloads(fileId: string, opts: FileDownloadOptions = {}) {
  return postFileAction(fileId, "save-to-downloads", opts);
}

export function openFileExternal(fileId: string) {
  return postFileAction(fileId, "open-external");
}

export function revealFileInFileManager(fileId: string) {
  return postFileAction(fileId, "reveal");
}

// ---- todo providers / sync (external TODO platforms) ----
export async function listTodoProviders() {
  return json<{ ok: boolean; providers: import("../types").TodoProvider[] }>(`${BASE}/todo-providers`);
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
  return json<{ ok: boolean; entries: import("../types").TodoSyncEntry[] }>(`${BASE}/todos/sync-status`);
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

export async function uninstallMod(name: string) {
  return json<{
    ok: boolean;
    error?: string;
    removed?: { name: string; path: string; scope: string };
  }>(`${BASE}/mods/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function validateMod(name: string) {
  return json<Record<string, unknown>>(`${BASE}/mods/validate`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ name }),
  });
}

export async function getSettings() {
  return jsonStrict<Record<string, unknown>>(`${BASE}/settings`);
}
export async function putSettings(data: Record<string, unknown>) {
  return jsonStrict<{ ok: boolean }>(`${BASE}/settings`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(data),
  });
}
// MCP 状态（结构化设置页契约）。enabled / disabledTools / toolDetails 由并行
// 的后端改造逐步补齐——前端一律可选读取，字段缺失时优雅降级到旧行为
// （缺 enabled 视为启用、缺 disabledTools 视为全启用、缺 toolDetails 只展示
// getMcp().tools 的名字列表）。
export type McpToolDetail = {
  name: string;
  /** graph 面向全名（运行时 ASCII slug）；有它就不要本地拼。 */
  fullName?: string | null;
  description?: string | null;
  /** 服务端 annotations.readOnlyHint */
  readOnly?: boolean;
  /** 服务端 annotations.destructiveHint */
  destructive?: boolean;
};
export type McpServerStatus = {
  name: string;
  /** 图面前缀（ASCII slug，运行时 naming.py 下发）。中文服务器名必须用它，
   *  不能拿 name 自己拼 `mcp_<name>_`。 */
  toolPrefix?: string;
  connected: boolean;
  tools: number;
  error?: string | null;
  enabled?: boolean;
  /** 本 server 被关掉的 tool 短名（不带 mcp_<server>_ 前缀） */
  disabledTools?: string[];
  /** 仅 GET /api/mcp?tools=1 时返回（设计文档 §4.1）；字段缺失时 UI 降级为纯名单 */
  toolDetails?: McpToolDetail[];
  /** 归一化后的 transport / 脱敏后的 url（卡片副行只吃这个，不自行拼原始 URL） */
  transport?: string;
  url?: string;
  /** 连接成功/最近失败的 wall-clock 时间戳（ms）——「connected for 3d」类 meta 的来源 */
  connectedAt?: number;
  lastErrorAt?: number;
};
// tools=true → ?tools=1（详情页 Tools tab / 工具明细）；5s 轮询保持无参轻查询。
// 后端未支持该参数时忽略之，前端按字段缺失降级。
export async function getMcp(opts?: { tools?: boolean }) {
  return json<{ servers: string[]; tools: string[]; failed?: string[]; status?: McpServerStatus[] }>(
    `${BASE}/mcp${opts?.tools ? "?tools=1" : ""}`,
  );
}
// parseError 非空 = mcp.json 损坏（后端拒绝伪装成空配置）。此时 mcpServers
// 恒为空对象，UI 必须锁写并报警，否则读改写会把原配置整个覆盖掉。
export async function getMcpConfig() {
  return json<{ mcpServers: Record<string, unknown>; parseError?: string }>(`${BASE}/mcp/config`);
}
export async function putMcp(data: unknown) {
  return json<{ ok: boolean }>(`${BASE}/mcp`, { method: "PUT", headers: H, body: JSON.stringify(data) });
}
export async function reloadMcp() {
  return json<{ ok: boolean; servers: string[] }>(`${BASE}/mcp/reload`, { method: "POST" });
}
// server 可选：带 ?server=<name> 时只重连该 server（绕过 120s 冷却）。
// 已连接/已禁用名幂等 no-op；未配置名返回 {ok:false, error}。
export async function reconnectMcp(server?: string) {
  const q = server ? `?server=${encodeURIComponent(server)}` : "";
  return json<{ ok: boolean; error?: string | null; status?: McpServerStatus[] }>(
    `${BASE}/mcp/reconnect${q}`,
    { method: "POST" },
  );
}
// 向导「测试连接」：对未保存 draft 的一次性真试连（initialize + tools/list
// 即拆，不落盘不碰 registry）。HTTP 200 恒定，ok:false 带 provider 原始
// 错误全文——接口错误永远亮给前端（2026-10-27）。
export async function testMcp(entry: unknown, timeout?: number) {
  return json<{ ok: boolean; tools?: string[]; count?: number; error?: string }>(
    `${BASE}/mcp/test`,
    {
      method: "POST",
      headers: H,
      body: JSON.stringify({ entry, timeout }),
    },
  );
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

// ---- Session Tool Policy（2026-11-12 P1）----

/** GET /api/mcp 轻负载（server 名 + 工具数）；includeTools 时带 toolDetails。 */
export async function mcpStatus(includeTools = false): Promise<{
  servers: string[]; status?: Array<Record<string, unknown>>;
}> {
  return json(`${BASE}/mcp${includeTools ? "?tools=1" : ""}`);
}

export type ContextToolPart = {
  server: string;
  connected: boolean;
  enabled: boolean;
  error: string;
  tools: number;
  enabledTools: number;
  tokens: number;
  disabledTokens: number;
  byTool: { name: string; tokens: number; enabled: boolean }[];
};

export type ContextBreakdown = {
  ok: boolean;
  error?: string;
  used: number;
  measured: boolean;
  window: number;
  threshold: number;
  counter: string;
  tools: ContextToolPart[];
  toolsTotal: number;
  skills: { count: number; disabled: number; tokens: number };
  system: number;
  history: number;
  fixed: number;
  policy: {
    mode: string;
    mcp_disabled: string[];
    mcp_tools_disabled: Record<string, string[]>;
    skills_disabled: string[];
  };
};

/** GET /api/sessions/{id}/context_breakdown —— 上下文账本（本地估算，≈）。 */
export async function contextBreakdown(sessionId: string): Promise<ContextBreakdown> {
  return json(`${BASE}/sessions/${sessionId}/context_breakdown`);
}

/** PUT /api/sessions/{id}/tool_policy —— 下一 turn 生效。 */
export async function setToolPolicy(
  sessionId: string,
  body: {
    mode: string;
    mcp_disabled: string[];
    mcp_tools_disabled?: Record<string, string[]>;
    skills_disabled: string[];
  },
): Promise<{ ok: boolean; error?: string }> {
  return json(`${BASE}/sessions/${sessionId}/tool_policy`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify(body),
  });
}

/** GET /api/sessions/{id}（会话详情；Tool Policy 等元信息读取用）。 */
export async function getSessionDetail(sessionId: string): Promise<Record<string, unknown>> {
  return json(`${BASE}/sessions/${sessionId}`);
}
