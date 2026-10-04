/**
 * Pure helpers for the code-panel file tree (docs/code-panel-design.md §3.4).
 *
 * The tree is lazy (one directory level per request) and stateful (which dirs
 * are expanded, which are loaded, how many rows past the 300-row cap the user
 * has revealed). Keeping that as a plain reducer here — independent of React
 * and of the network — lets the component stay thin and keeps the ordering /
 * flattening rules testable.
 */

import type { CodeEntry } from "@/lib/codeTypes";
import { currentLocale } from "@/i18n/provider";
import codeEn from "../../../../messages/en/code.json";
import codeZh from "../../../../messages/zh-CN/code.json";

/** One rendered level is capped at 300 rows; "显示更多" reveals another batch
 *  (design §4.1: no virtualization, so bound the DOM instead). */
export const ROW_LIMIT = 300;

/** Server-side per-level entry cap (`list` sets `truncated` past this). */
export const DIR_ENTRY_CAP = 2000;

/** Directory-first, then case/numeric-aware name ordering — so `file2` sorts
 *  before `file10` (design §3.4). */
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

export function sortEntries(entries: CodeEntry[]): CodeEntry[] {
  return [...entries].sort((a, b) => {
    const ad = a.type === "dir" ? 0 : 1;
    const bd = b.type === "dir" ? 0 : 1;
    if (ad !== bd) return ad - bd;
    return collator.compare(a.name, b.name);
  });
}

/** Accumulate a child path from its parent directory path. `""` is the root. */
export function joinPath(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name;
}

/** The parent directory path of a root-relative path (`""` at the top). */
export function parentPath(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

/** Ancestor directory paths of a file path, outermost first:
 *  `"src/api/main.py"` → `["src", "src/api"]`. Used to reveal a file. */
export function ancestorDirs(path: string): string[] {
  if (!path) return [];
  const parts = path.split("/").filter(Boolean);
  const out: string[] = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join("/"));
  return out;
}

/** Stable DOM id for a tree row (paths may contain spaces, hence the escape). */
export function rowId(path: string): string {
  return `code-row-${encodeURIComponent(path)}`;
}

// ---- 文案：非 hook 读取器（i18n-design.md §4.4）-----------------------------
//
// 本模块是纯函数库，拿不到 useTranslations（hook 只能在组件渲染期用）。架构同
// i18n/uiText.ts：静态 import 双语言 catalog + 复用 provider 暴露的模块级
// locale 镜像 currentLocale()（provider 渲染期同步赋值，locale 切换后下一次
// 渲染即读到新文案）；SSG 构建期无 provider，默认 en，与预渲染 HTML 一致。

const CODE_CATALOGS: Record<string, Record<string, unknown>> = {
  en: codeEn.code,
  "zh-CN": codeZh.code,
};

/** 按 a.b.c 路径取 code 域叶子字符串，支持 {name} 简单插值；fallback 链与
 *  next-intl 一致：当前 locale → en → key 原样。 */
function codeText(key: string, params?: Record<string, string | number>): string {
  const lookup = (tree: Record<string, unknown>): string | undefined => {
    let node: unknown = tree;
    for (const seg of key.split(".")) {
      if (typeof node !== "object" || node === null) return undefined;
      node = (node as Record<string, unknown>)[seg];
    }
    return typeof node === "string" ? node : undefined;
  };
  const raw = lookup(CODE_CATALOGS[currentLocale()]) ?? lookup(CODE_CATALOGS.en) ?? key;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (m, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : m,
  );
}

/** Error code → localized copy（catalog: code.errors.*，append-only，design §4.3）.
 *  Used by the tree's inline failure state and shared with the rest of the panel. */
type ErrorKey = keyof typeof codeEn.code.errors;

const ERROR_KEY_OF: Record<string, ErrorKey> = {
  "unknown-root": "unknown-root",
  "root-missing": "root-missing",
  "outside-root": "outside-root",
  "denied-path": "denied-path",
  "not-directory": "not-directory",
  binary: "binary",
  "not-text": "not-text",
  "too-large": "too-large",
  truncated: "truncated",
  "read-only-mount": "read-only-mount",
  "git-internal": "git-internal",
  // S4 (brief §3.1 / design §4.3, append-only): the content search gave up on a
  // budget instead of finishing, and the caller-side mistakes the search
  // endpoint reports at HTTP 200 (invalid-mode / invalid-query / invalid-regex
  // are already in `runtime.ts`'s `CodeErrorCode`).
  "search-timeout": "search-timeout",
  "invalid-mode": "invalid-mode",
  "invalid-query": "invalid-query",
  "invalid-regex": "invalid-regex",
};

export function codeErrorMessage(code: string | null | undefined): string {
  const key = code ? ERROR_KEY_OF[code] : undefined;
  if (!key) return codeText("errors.fallback");
  if (key === "truncated") return codeText("errors.truncated", { count: DIR_ENTRY_CAP });
  return codeText(`errors.${key}`);
}

/** Compact byte size, matching SessionFilesSettings' formatting. */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "";
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

// ---- git / agent decorations (design §3.4, S3) ------------------------------
//
// Both decorations are keyed by the row's FULL root-relative path — the very key
// `flattenTree` builds with `joinPath`, and the one `/api/code/git` returns
// (the server rebases entries when a root is a repo subdirectory). Callers must
// look up with `row.path`, never with a hand-concatenated string.
//
// They intentionally use DIFFERENT encodings (brief §1-4): git is a letter at
// the start of the row, an agent change is an accent bar + dot in the gutter.
// Neither may replace the other; a row can carry both.

/** Row-start git badge: the porcelain letter, a colour, a localized tooltip. */
export interface GitBadge {
  /** The letter as rendered (`M` `A` `U` `D` `R` `C` `!`). */
  letter: string;
  /** Tailwind text-colour class. */
  className: string;
  /** Localized tooltip explaining the status (code.git.*). */
  title: string;
}

/** Colour classes are verified against `tailwind.config.ts`: the palette has
 *  `yellow`/`green`/`red`/`blue`/`violet`/`faint`/`muted` but NO `amber` or
 *  `cyan` token (`theme.extend.colors` leaves Tailwind's default *palette*
 *  objects, so `text-amber` / `text-cyan` compile to nothing and silently lose
 *  their colour). Those two therefore use literal hex values. */
type GitTipKey = keyof typeof codeEn.code.git;

const GIT_BADGE: Record<string, { className: string; tip: GitTipKey }> = {
  M: { className: "text-[#f59e0b]", tip: "modified" },
  A: { className: "text-green", tip: "added" },
  U: { className: "text-green", tip: "added" },
  "?": { className: "text-green", tip: "untracked" },
  D: { className: "text-red", tip: "deleted" },
  R: { className: "text-blue", tip: "renamed" },
  C: { className: "text-[#06b6d4]", tip: "copied" },
  "!": { className: "text-faint", tip: "ignored" },
};

/** git porcelain code → badge, or `null` for "no decoration". Unknown letters
 *  still render (demoted, with a generic tooltip) rather than being swallowed. */
export function gitBadgeOf(code: string | null | undefined): GitBadge | null {
  const letter = code?.trim().slice(0, 1) ?? "";
  if (!letter) return null;
  const known = GIT_BADGE[letter.toUpperCase()];
  return known
    ? { letter, className: known.className, title: codeText(`git.${known.tip}`) }
    : { letter, className: "text-muted", title: codeText("git.unknown", { letter }) };
}

/** Left-gutter agent-change marker (bar + dot) — shape kept as an object so the
 *  marker can grow without touching every call site. */
export interface AgentMarker {
  /** Localized tooltip naming the operation (code.agent.*). */
  title: string;
}

/** Agent change op → marker, or `null` for "no decoration". */
export function agentMarkerOf(op: "write" | "edit" | null | undefined): AgentMarker | null {
  if (op !== "write" && op !== "edit") return null;
  return { title: codeText(op === "edit" ? "agent.edit" : "agent.write") };
}

// ---- expand / load state machine -------------------------------------------

export interface TreeState {
  /** path → loaded (and sorted) entries. */
  listings: Record<string, CodeEntry[]>;
  /** path → server reported its level was cut at DIR_ENTRY_CAP. */
  truncated: Record<string, boolean>;
  /** path → a request is in flight (with no entries yet → skeleton row). */
  loading: Record<string, boolean>;
  /** path → last failure code. Cleared on a successful load. */
  errors: Record<string, string>;
  /** path → expanded? (`""` is the root itself). */
  open: Record<string, boolean>;
  /** path → how many of the level's rows are revealed (default ROW_LIMIT). */
  visibleCount: Record<string, number>;
}

export type TreeAction =
  | { type: "reset" }
  | { type: "open"; path: string }
  | { type: "close"; path: string }
  | { type: "toggle"; path: string }
  | { type: "loadStart"; path: string }
  | { type: "loadOk"; path: string; entries: CodeEntry[]; truncated: boolean }
  | { type: "loadError"; path: string; code: string }
  | { type: "more"; path: string };

export function initialTreeState(): TreeState {
  return { listings: {}, truncated: {}, loading: {}, errors: {}, open: {}, visibleCount: {} };
}

export function treeReducer(state: TreeState, action: TreeAction): TreeState {
  switch (action.type) {
    case "reset":
      return initialTreeState();
    case "open":
      return { ...state, open: { ...state.open, [action.path]: true } };
    case "close":
      return { ...state, open: { ...state.open, [action.path]: false } };
    case "toggle":
      return { ...state, open: { ...state.open, [action.path]: !state.open[action.path] } };
    case "loadStart":
      return { ...state, loading: { ...state.loading, [action.path]: true } };
    case "loadOk": {
      const errors = { ...state.errors };
      delete errors[action.path];
      return {
        ...state,
        listings: { ...state.listings, [action.path]: sortEntries(action.entries) },
        truncated: { ...state.truncated, [action.path]: action.truncated },
        loading: { ...state.loading, [action.path]: false },
        errors,
        visibleCount: {
          ...state.visibleCount,
          [action.path]: state.visibleCount[action.path] ?? ROW_LIMIT,
        },
      };
    }
    case "loadError":
      return {
        ...state,
        loading: { ...state.loading, [action.path]: false },
        errors: { ...state.errors, [action.path]: action.code },
      };
    case "more":
      return {
        ...state,
        visibleCount: {
          ...state.visibleCount,
          [action.path]: (state.visibleCount[action.path] ?? ROW_LIMIT) + ROW_LIMIT,
        },
      };
    default:
      return state;
  }
}

// ---- flattening ------------------------------------------------------------

export interface TreeRow {
  path: string;
  name: string;
  depth: number;
  type: CodeEntry["type"];
  /** Heavy directory the server flagged — rendered as a placeholder that opens. */
  hidden: boolean;
  size: number | null;
  /** Dirs and heavy-dir placeholders both expand. */
  expandable: boolean;
}

export type TreeItem =
  | { kind: "row"; id: string; row: TreeRow }
  | { kind: "loading"; id: string; path: string; depth: number }
  | { kind: "error"; id: string; path: string; depth: number; code: string }
  | { kind: "empty"; id: string; path: string; depth: number }
  | { kind: "more"; id: string; path: string; depth: number; remaining: number }
  | { kind: "truncated"; id: string; path: string; depth: number };

/** Depth-first flatten of the expanded tree, honoring the row caps and the
 *  loading / error / empty / truncated decorations. Only expanded dirs recurse. */
export function flattenTree(state: TreeState, rootPath = ""): TreeItem[] {
  const items: TreeItem[] = [];

  const walk = (dirPath: string, depth: number): void => {
    const entries = state.listings[dirPath];
    const infoId = `code-info-${encodeURIComponent(dirPath)}`;

    if (!entries) {
      if (state.loading[dirPath]) {
        items.push({ kind: "loading", id: `${infoId}-load`, path: dirPath, depth });
      } else if (state.errors[dirPath]) {
        items.push({
          kind: "error",
          id: `${infoId}-err`,
          path: dirPath,
          depth,
          code: state.errors[dirPath],
        });
      }
      return;
    }
    if (!entries.length) {
      items.push({ kind: "empty", id: `${infoId}-empty`, path: dirPath, depth });
      return;
    }

    const cap = state.visibleCount[dirPath] ?? ROW_LIMIT;
    for (const e of entries.slice(0, cap)) {
      const sub = joinPath(dirPath, e.name);
      const expandable = e.type === "dir" || e.hidden;
      items.push({
        kind: "row",
        id: rowId(sub),
        row: {
          path: sub,
          name: e.name,
          depth,
          type: e.type,
          hidden: e.hidden,
          size: e.size,
          expandable,
        },
      });
      if (expandable && state.open[sub]) walk(sub, depth + 1);
    }

    if (entries.length > cap) {
      items.push({
        kind: "more",
        id: `${infoId}-more`,
        path: dirPath,
        depth,
        remaining: entries.length - cap,
      });
    }
    if (state.truncated[dirPath]) {
      items.push({ kind: "truncated", id: `${infoId}-trunc`, path: dirPath, depth });
    }
  };

  walk(rootPath, 0);
  return items;
}

// ---- S4: drag/drop and create/rename helpers --------------------------------
//
// Pure, React-free predicates so the file-op rules live in one place and the
// component only wires them to the DOM. The server re-checks everything anyway
// (`resolve_code_target(..., write=True)` + the Rust-side containment check);
// these exist to stop a doomed request before it is sent and to render an honest
// "why not" in the UI.

/** True when `path` IS `ancestor` or lives beneath it. `ancestor === ""` is the
 *  root, which contains everything. Used to refuse moving a directory into its
 *  own subtree (which would detach it from the tree entirely). */
export function isSelfOrDescendant(path: string, ancestor: string): boolean {
  if (ancestor === "") return true;
  return path === ancestor || path.startsWith(`${ancestor}/`);
}

/** Validate a single path segment for create/rename (brief §1-6 / design §4.2):
 *  non-empty, no `/`, not `.` / `..`, no control characters, ≤255 bytes.
 *  Returns a localized message (code.nameValidation.*), or `null` when the
 *  name is acceptable. */
export function validateEntryName(name: string): string | null {
  const n = name.trim();
  if (!n) return codeText("nameValidation.empty");
  if (n.includes("/")) return codeText("nameValidation.slash");
  if (n === "." || n === "..") return codeText("nameValidation.dot");
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(n)) return codeText("nameValidation.control");
  if (new TextEncoder().encode(n).length > 255) return codeText("nameValidation.tooLong");
  return null;
}

/** The last segment of a root-relative path (e.g. `"src/a.py"` → `"a.py"`). */
export function baseName(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? path : path.slice(i + 1);
}

/** Whether a drop of `dragPath` onto `targetDir` is allowed. `targetDir` is the
 *  hovered DIRECTORY (root-relative, `""` = root); `null` means "no directory
 *  under the pointer". Directories may not be dropped into themselves. */
export function canDropInto(dragPath: string, targetDir: string | null): boolean {
  if (targetDir == null) return false;
  if (targetDir === "") return true;
  return targetDir !== dragPath && !isSelfOrDescendant(targetDir, dragPath);
}

/** Keyboard navigation over the flattened items (rows only are focusable):
 *  returns the row key `dir` steps from `currentKey`, clamped to the list. */
export function moveRow(
  items: TreeItem[],
  currentKey: string | null,
  dir: 1 | -1,
): string | null {
  const rows = items.filter((i): i is Extract<TreeItem, { kind: "row" }> => i.kind === "row");
  if (!rows.length) return null;
  const idx = currentKey ? rows.findIndex((r) => r.row.path === currentKey) : -1;
  if (idx < 0) return dir === 1 ? rows[0].row.path : rows[rows.length - 1].row.path;
  const next = Math.max(0, Math.min(rows.length - 1, idx + dir));
  return rows[next].row.path;
}