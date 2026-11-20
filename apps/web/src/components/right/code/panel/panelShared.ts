/**
 * Shared constants, pure helpers and types for the code panel family
 * (CodePanel + panel/* hooks + subcomponents).
 *
 * Kept dependency-light on purpose: everything here is importable from the
 * node-environment vitest suite (no React, no Tauri) — see panelShared.test.ts.
 * Extracted from CodePanel.tsx in the hooks split (批次 3-3); behaviour is
 * byte-for-byte the logic that used to live at module scope there.
 */

import { CodeApiError } from "@/lib/runtime";
import type { CodeRead } from "@/lib/codeTypes";

/** Panel width at/above which the tree+editor "side" layout is allowed. */
export const WIDE_PX = 600;
/** Fixed tree column width in "side" mode (design §3.3: 220–300px). */
export const TREE_PX = 260;
/** The web app is single-project; same convention as listArtifacts/listSkills. */
export const PROJECT_SLUG = "default";

/** Open-tab cap (design §4.7). Eviction is LRU over CLEAN tabs only. */
export const MAX_TABS = 20;

/** Message for a failed file op. Prefer the server's own Chinese wording — it
 *  names the real reason ("当前文件夹以只读方式挂载", "已存在同名条目", …), which a
 *  generic fallback would throw away. */
export function fsError(err: unknown, fallback: string): string {
  if (err instanceof CodeApiError) return err.message || err.code;
  return err instanceof Error ? err.message : fallback;
}

/** i18n-design.md §7 契约:Rust code_* 命令拒绝值是 {code, detail} 对象
 *  (壳不持文案)。code 是稳定标识符 → code.notice.err.<code>;detail 是
 *  OS/路径原始上下文,原样插值。旧壳的 string 错误/其它形状返回 null,
 *  由调用方走既有 fallback 路径。 */
export function codeCmdErrParts(err: unknown): { code: string; detail: string } | null {
  if (err && typeof err === "object" && typeof (err as { code?: unknown }).code === "string") {
    const detail = (err as { detail?: unknown }).detail;
    return {
      code: (err as { code: string }).code,
      detail: typeof detail === "string" ? detail : "",
    };
  }
  return null;
}

/** Escape user text so it matches LITERALLY inside the server's regex.
 *  Content search compiles `q` as a regex while the result highlight compares
 *  literal text (SearchView.Highlighted); leaving `foo.bar` unescaped would
 *  match `fooXbar` and highlight nothing, and `foo(` would fail with
 *  `invalid-regex`. Regex stays available at the API layer for a future toggle. */
export function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Editing state for one open tab (S2). */
export interface TabMeta {
  /** Unsaved edits in the buffer. */
  dirty: boolean;
  /** A save was refused — or the agent changed the file underneath — and the
   *  panel must not overwrite it. A 409 fills both texts; a PUSHED change
   *  carries only a version, so `diskText` may be null and 「看差异」 fetches it
   *  on demand (S3 brief §0). */
  conflict: { version: string; diskText: string | null; mine: string } | null;
  /** Bumped to REBUILD the editor's model (see the comment in the component). */
  rev: number;
  /** Bumped to tell Monaco "the buffer is the new clean baseline". */
  savedNonce: number;
  /** View mode for renderable pages (html/htm). Absent = automatic, i.e.
   *  preview for `isRenderableHtml` paths and source for everything else;
   *  the toggle writes an explicit value. Only the auto case is ever "true by
   *  default", so non-HTML files never grow state. */
  preview?: boolean;
}

export const EMPTY_TAB_META: TabMeta = { dirty: false, conflict: null, rev: 0, savedNonce: 0 };

export interface ReadState {
  status: "loading" | "ok" | "error";
  data?: CodeRead;
  error?: string;
}

/** A pending reveal: which root/path to focus in the tree and, when jumping
 *  from chat, which line to centre in Monaco. */
export interface Jump {
  rootId: string;
  path: string;
  line: number | null;
  nonce: number;
}

/** Inline-diff overlay payload (code-panel-diff-design.md §B): session
 *  baseline vs current disk content for a CLEAN tab the agent rewrote.
 *  The STATE lives in useCodeTabs; useCodeDiff writes it. */
export interface InlineDiffState {
  key: string;
  base: string;
  current: string;
  baseVersion: string;
  currentVersion: string;
  degraded: boolean;
}
