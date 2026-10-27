/**
 * Context folders, artifacts, files (upload / preview), todo
 * providers, settings / MCP / skills and mods.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";
import { uiText } from "../../i18n/uiText";
// 非 hook 层取已解析 locale（模块级镜像，provider 渲染期同步赋值）：随每个
// API 请求/WS 连接下发给 runtime（i18n-design.md §1/§5）。
import { currentLocale } from "../../i18n/provider";
import type { Todo } from "../types";

// ---- context folders (docs/context-folders-design.md) ----
export async function listFolders() {
  return json<{ ok: boolean; folders: import("../types").FolderEntry[] }>(`${BASE}/folders`);
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
export async function getArtifactMetadata(id: string, project_slug = "default") {
  return json<import("../types").ArtifactMeta>(
    `${BASE}/artifacts/${id}/metadata?project_slug=${project_slug}`,
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
export async function uploadFile(sessionId: string, file: File) {
  const fd = new FormData();
  fd.append("session_id", sessionId);
  fd.append("file", file);
  // NOTE: no Content-Type header — the browser sets the multipart boundary.
  return json<{ ok: boolean; error?: string; file?: import("../types").FileEntry }>(
    `${BASE}/files`,
    { method: "POST", body: fd },
  );
}

export async function listFiles(project_slug = "default", session_id?: string) {
  const q = session_id ? `&session_id=${encodeURIComponent(session_id)}` : "";
  return json<import("../types").FileEntry[]>(`${BASE}/files?project_slug=${project_slug}${q}`);
}

// Attach an OS file by native path (Tauri desktop drag & drop — WKWebView can't
// expose dropped files to JS, so the shell forwards the path and the sidecar
// copies + registers it). Returns the same shape as uploadFile.
export async function attachFilePath(sessionId: string, path: string) {
  return json<{ ok: boolean; error?: string; file?: import("../types").FileEntry }>(
    `${BASE}/files/attach-path`,
    { method: "POST", headers: H, body: JSON.stringify({ session_id: sessionId, path }) },
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
  return json<Record<string, unknown>>(`${BASE}/settings`);
}
export async function putSettings(data: Record<string, unknown>) {
  return json<{ ok: boolean }>(`${BASE}/settings`, {
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
