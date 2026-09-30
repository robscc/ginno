/**
 * Wire types for the code panel (docs/code-panel-design.md §3.2 / §4.1).
 *
 * These mirror the JSON the runtime returns from /api/code/{roots,list,read};
 * the server never accepts an absolute path — a root `id` plus a root-relative
 * `path` is the whole address space (design §4.3).
 */

export interface CodeRoot {
  id: string; name: string; path: string;
  access: "ro" | "rw"; missing: boolean;
  is_repo: boolean; branch: string | null;
}
export interface CodeEntry {
  name: string; type: "dir" | "file" | "symlink" | "other";
  size: number | null; mtime: number | null; hidden: boolean;
  /** Immediate children of a heavy dir (its placeholder row's count); `null`
   *  for every other entry. Capped at the server's `HIDDEN_COUNT_CAP` (9999),
   *  which doubles as the "at least this many" sentinel. */
  hidden_count: number | null;
}
/** Per-root git status (design §4.7 S3).
 *
 * `entries` is keyed by ROOT-RELATIVE path so a tree row can look itself up
 * directly — the server rebases paths when the root is a repo subdirectory. */
export interface CodeGitStatus {
  is_repo: boolean;
  toplevel: string | null;
  branch: string | null;
  /** root-relative path → one of M A U D R C ! */
  entries: Record<string, string>;
}

export interface CodeListing {
  root: string; path: string; entries: CodeEntry[]; truncated: boolean;
}
export interface CodeRead {
  root: string; path: string; version: string; encoding: string;
  language: string; editable: boolean;
  /** Why the viewer is read-only. `"image"` is the one value that is not a
   *  degradation: the panel streams the bytes and shows a picture instead of
   *  text (the runtime decides it from the repo's own IMAGE_EXTS). */
  readonly_reason:
    | "read-only-mount" | "too-large" | "binary" | "git-internal" | "image" | null;
  text: string | null; eof: boolean;
}