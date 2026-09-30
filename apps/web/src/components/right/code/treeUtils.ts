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

/** Error code → Chinese copy (append-only, design §4.3). Used by the tree's
 *  inline failure state and shared with the rest of the panel. */
const ERROR_TEXT: Record<string, string> = {
  "unknown-root": "未知的工作区根",
  "root-missing": "文件夹不存在（已移动或删除）",
  "outside-root": "该文件不在当前工作区根内",
  "denied-path": "该位置不可访问（已被保护）",
  "not-directory": "这不是一个文件夹",
  binary: "二进制文件，无法以文本打开",
  "not-text": "无法识别文本编码",
  "too-large": "文件过大",
  truncated: `仅显示前 ${DIR_ENTRY_CAP} 项`,
  "read-only-mount": "此文件夹以只读方式挂载",
  "git-internal": "Git 内部文件，只读",
  // S4 (brief §3.1 / design §4.3, append-only): the content search gave up on a
  // budget instead of finishing, and the caller-side mistakes the search
  // endpoint reports at HTTP 200 (invalid-mode / invalid-query / invalid-regex
  // are already in `runtime.ts`'s `CodeErrorCode`).
  "search-timeout": "搜索超时，结果可能不完整",
  "invalid-mode": "无效的搜索模式",
  "invalid-query": "搜索内容无效（过短或过长）",
  "invalid-regex": "正则表达式无效",
};

export function codeErrorMessage(code: string | null | undefined): string {
  if (!code) return "加载失败";
  return ERROR_TEXT[code] ?? "加载失败";
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

/** Row-start git badge: the porcelain letter, a colour, a Chinese tooltip. */
export interface GitBadge {
  /** The letter as rendered (`M` `A` `U` `D` `R` `C` `!`). */
  letter: string;
  /** Tailwind text-colour class. */
  className: string;
  /** Chinese tooltip explaining the status. */
  title: string;
}

/** Colour classes are verified against `tailwind.config.ts`: the palette has
 *  `yellow`/`green`/`red`/`blue`/`violet`/`faint`/`muted` but NO `amber` or
 *  `cyan` token (`theme.extend.colors` leaves Tailwind's default *palette*
 *  objects, so `text-amber` / `text-cyan` compile to nothing and silently lose
 *  their colour). Those two therefore use literal hex values. */
const GIT_BADGE: Record<string, Omit<GitBadge, "letter">> = {
  M: { className: "text-[#f59e0b]", title: "已修改，未提交" },
  A: { className: "text-green", title: "新增文件，未提交" },
  U: { className: "text-green", title: "新增文件，未提交" },
  "?": { className: "text-green", title: "新增文件，未被 Git 跟踪" },
  D: { className: "text-red", title: "已删除，未提交" },
  R: { className: "text-blue", title: "已重命名，未提交" },
  C: { className: "text-[#06b6d4]", title: "已复制，未提交" },
  "!": { className: "text-faint", title: "已被 .gitignore 忽略" },
};

/** git porcelain code → badge, or `null` for "no decoration". Unknown letters
 *  still render (demoted, with a generic tooltip) rather than being swallowed. */
export function gitBadgeOf(code: string | null | undefined): GitBadge | null {
  const letter = code?.trim().slice(0, 1) ?? "";
  if (!letter) return null;
  const known = GIT_BADGE[letter.toUpperCase()];
  return known
    ? { letter, ...known }
    : { letter, className: "text-muted", title: `Git 状态：${letter}` };
}

/** Left-gutter agent-change marker (bar + dot) — shape kept as an object so the
 *  marker can grow without touching every call site. */
export interface AgentMarker {
  /** Chinese tooltip naming the operation. */
  title: string;
}

/** Agent change op → marker, or `null` for "no decoration". */
export function agentMarkerOf(op: "write" | "edit" | null | undefined): AgentMarker | null {
  if (op !== "write" && op !== "edit") return null;
  return { title: op === "edit" ? "agent 改过这个文件（编辑）" : "agent 改过这个文件（写入）" };
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
 *  Returns a Chinese message, or `null` when the name is acceptable. */
export function validateEntryName(name: string): string | null {
  const n = name.trim();
  if (!n) return "名称不能为空";
  if (n.includes("/")) return "名称不能包含 /";
  if (n === "." || n === "..") return "名称不能是 . 或 ..";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(n)) return "名称不能包含控制字符";
  if (new TextEncoder().encode(n).length > 255) return "名称过长（最多 255 字节）";
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