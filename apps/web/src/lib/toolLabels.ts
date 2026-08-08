/**
 * Tool display labels — friendly names for tool call bubbles.
 *
 * Resolution order:
 *   1. User-configured mapping (settings.json → tool_labels) — wins over defaults
 *   2. Built-in default labels (below) — always available, no settings needed
 *   3. MCP auto-detection: mcp_{server}_{tool} → "正在调用MCP：{server}"
 *   4. Raw tool name fallback
 *
 * The built-in defaults matter because settings.json only carries a
 * ``tool_labels`` key if it was created after the feature shipped (or the user
 * edited it) — pre-existing installs would otherwise fall back to raw names.
 *
 * Labels are loaded once from the settings API and cached at module level.
 * Call `loadToolLabels()` at app startup (or when settings change) to populate
 * the cache; `toolLabel(name)` is synchronous and reads from cache.
 */

import { getSettings } from "./runtime";

/** Built-in friendly labels for the common tools. Settings override these. */
export const DEFAULT_TOOL_LABELS: Record<string, string> = {
  read_file: "读取文件中",
  write_file: "写文件中",
  edit_file: "编辑文件中",
  glob_files: "搜索文件中",
  grep_files: "搜索内容中",
  bash: "执行命令中",
  parse_document: "解析文档中",
  analyze_table: "分析表格中",
};

// Module-level cache: defaults merged with user settings (settings win).
let _labels: Record<string, string> = { ...DEFAULT_TOOL_LABELS };
let _loaded = false;

/** Fetch settings and populate the label cache (defaults + user overrides). */
export async function loadToolLabels(): Promise<void> {
  let user: Record<string, string> = {};
  try {
    const s = await getSettings();
    user = (s.tool_labels as Record<string, string>) || {};
  } catch {
    // Settings API unavailable — keep the built-in defaults only.
  }
  _labels = { ...DEFAULT_TOOL_LABELS, ...user };
  _loaded = true;
}

/** Refresh labels (call after settings are saved). */
export async function refreshToolLabels(): Promise<void> {
  await loadToolLabels();
}

/** Synchronous label lookup. Returns the friendly name for a tool. */
export function toolLabel(name: string): string {
  // 1. User-configured mapping, or 2. built-in default
  if (_labels[name]) return _labels[name];

  // 3. MCP auto-detection: mcp_{server}_{tool} → "正在调用MCP：{server}"
  if (name.startsWith("mcp_")) {
    const parts = name.split("_");
    // Format: mcp_{server}_{rest...} — server is parts[1]
    if (parts.length >= 3) {
      return `正在调用MCP：${parts[1]}`;
    }
  }

  // 4. Raw name fallback
  return name;
}

/** Whether the label cache has been loaded at least once. */
export function isLabelsLoaded(): boolean {
  return _loaded;
}
