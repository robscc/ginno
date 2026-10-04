/**
 * Tool display labels — friendly names for tool call bubbles.
 *
 * Resolution order:
 *   1. User-configured mapping (settings.json → tool_labels) — wins over defaults
 *   2. Built-in default labels (messages/{en,zh-CN}/tool.json) — per i18n locale
 *   3. MCP auto-detection: mcp_{server}_{tool} → "Calling MCP: {server}"
 *   4. Raw tool name fallback
 *
 * The built-in defaults matter because settings.json only carries a
 * ``tool_labels`` key if it was created after the feature shipped (or the user
 * edited it) — pre-existing installs would otherwise fall back to raw names.
 *
 * Labels are loaded once from the settings API and cached at module level.
 * Call `loadToolLabels()` at app startup (or when settings change) to populate
 * the cache; `toolLabel(name)` is synchronous and reads from cache. Locale
 * switches rebuild the cache via `setToolLabelsLocale()` (called by the i18n
 * provider) — user overrides in settings keep priority over catalog defaults.
 */

import enToolJson from "../../messages/en/tool.json";
import zhToolJson from "../../messages/zh-CN/tool.json";
import type { Locale } from "../i18n/config";
import { getSettings } from "./runtime";

type ToolCatalog = { labels: Record<string, string>; mcpCall: string };

const TOOL_CATALOGS: Record<Locale, ToolCatalog> = {
  en: { labels: enToolJson.tool.labels, mcpCall: enToolJson.tool.mcp_call },
  "zh-CN": { labels: zhToolJson.tool.labels, mcpCall: zhToolJson.tool.mcp_call },
};

/** Built-in defaults (en source catalog). ToolLabelsSettings shows these merged
 * with user overrides so every label is visible/editable pre-customization. */
export const DEFAULT_TOOL_LABELS: Record<string, string> = { ...TOOL_CATALOGS.en.labels };

let _catalog: ToolCatalog = TOOL_CATALOGS.en;
let _user: Record<string, string> = {};
// Module-level cache: defaults merged with user settings (settings win).
let _labels: Record<string, string> = { ...TOOL_CATALOGS.en.labels };
let _loaded = false;

/** i18n locale 切换时由 provider 调用：默认标签换到目标 catalog，用户覆盖保持优先。 */
export function setToolLabelsLocale(locale: Locale): void {
  _catalog = TOOL_CATALOGS[locale] ?? TOOL_CATALOGS.en;
  _labels = { ..._catalog.labels, ..._user };
}

/**
 * Labels the runtime seeded into settings.json when the feature shipped
 * (paths.py `tool_labels` defaults, pre-English-UI). A stored value equal to
 * one of these counts as "never customized", so it migrates to the new
 * default instead of overriding it with stale Chinese.
 */
const LEGACY_SEEDED_LABELS: Record<string, string> = {
  读取文件中: "Reading file",
  写文件中: "Writing file",
  编辑文件中: "Editing file",
  搜索文件中: "Searching files",
  搜索内容中: "Searching content",
  执行命令中: "Running command",
  解析文档中: "Parsing document",
  分析表格中: "Analyzing table",
};

function migrateLegacySeeded(user: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(user)) {
    out[k] = v
      .split("|")
      .map((seg) => LEGACY_SEEDED_LABELS[seg.trim()] ?? seg.trim())
      .join("|");
  }
  return out;
}

/** Fetch settings and populate the label cache (defaults + user overrides). */
export async function loadToolLabels(): Promise<void> {
  try {
    const s = await getSettings();
    _user = migrateLegacySeeded((s.tool_labels as Record<string, string>) || {});
  } catch {
    // Settings API unavailable — keep the built-in defaults only.
    _user = {};
  }
  _labels = { ..._catalog.labels, ..._user };
  _loaded = true;
}

/** Refresh labels (call after settings are saved). */
export async function refreshToolLabels(): Promise<void> {
  await loadToolLabels();
}

/**
 * All candidate labels for a tool. A configured value may carry several
 * names separated by "|" (e.g. "Writing file|Saving file"); callers that want
 * variety (tool-call bubbles) pick one at random, others use the first.
 */
export function toolLabelOptions(name: string): string[] {
  let raw: string | undefined;
  // 1. User-configured mapping, or 2. built-in default for the current locale
  if (_labels[name]) raw = _labels[name];

  // 3. MCP auto-detection: mcp_{server}_{tool} → "Calling MCP: {server}"
  if (raw === undefined && name.startsWith("mcp_")) {
    const parts = name.split("_");
    // Format: mcp_{server}_{rest...} — server is parts[1]
    if (parts.length >= 3) {
      raw = _catalog.mcpCall.replace("{server}", parts[1]);
    }
  }

  // 4. Raw name fallback
  const opts = (raw ?? name).split("|").map((s) => s.trim()).filter(Boolean);
  return opts.length ? opts : [name];
}

/** Synchronous label lookup. Deterministic (first candidate) — safe to
 * call directly in render for stable UI like permission cards. */
export function toolLabel(name: string): string {
  return toolLabelOptions(name)[0];
}

/** Whether the label cache has been loaded at least once. */
export function isLabelsLoaded(): boolean {
  return _loaded;
}
