/**
 * Code panel: workspace tree, file reader/writer and file operations.
 *
 * Split out of lib/runtime.ts, which re-exports every name unchanged.
 */
import { BASE, H, json } from "./core";
import type { CodeEntry, CodeGitStatus, CodeListing, CodeRead, CodeRoot } from "../codeTypes";
// Type-only import: the search contract lives next to its views (SearchView),
// which is where both QuickOpen and SearchView read it from. Erased at compile,
// so this pulls no component code into the lib.
import type {
  CodeSearchHit,
  CodeSearchMode,
  CodeSearchResult,
} from "@/components/right/code/SearchView";

// ---- code panel (docs/code-panel-design.md) ----
// Read-only workspace tree + file reader. The server answers with the shared
// `{ ok, ... }` envelope at HTTP 200 (same convention as folders/files), even
// on failure: a bad root, a denied path, a binary/oversized file, etc. These
// wrappers unwrap the success shape and throw CodeApiError on `ok:false` so
// callers (and the panel UI) can branch on the error code.
//
// Endpoints are addressed by `root` id + root-relative `path` only — the
// client never sends an absolute path (design §4.3).

/** Server-side error code from a failed code-endpoint call (design §4.3).
 *
 *  An append-only list: the S1 fence codes plus whatever later stages added.
 *  Kept as a plain string on the wire; this union exists so the UI's copy map
 *  is exhaustive rather than silently falling back. */
export type CodeErrorCode =
  | "unknown-root"
  | "root-missing"
  | "outside-root"
  | "denied-path"
  | "not-directory"
  | "binary"
  | "not-text"
  | "too-large"
  | "truncated"
  | "read-only-mount"
  | "git-internal"
  | "absent"
  | "conflict"
  | "search-timeout"
  // S4 search: an unknown `mode`, an empty/oversized `q`, and a regex that will
  // not compile are caller mistakes — `{ok:false}` at HTTP 200, never a 500.
  | "invalid-mode"
  | "invalid-query"
  | "invalid-regex"
  // S4 file operations.
  | "bad-name" // rejects "/", ".", "..", control chars, >255 bytes
  | "exists" // the target name is taken
  | "root-immutable" // a root may not be renamed, moved or deleted
  | "confirm-required" // non-empty dir: call again with `confirm: true`
  | "invalid-target"; // e.g. moving a directory into its own subtree

/** Thrown by the code clients when the runtime replies `ok:false`. `code`
 *  carries the server error code so the UI can pick a message; `message` is
 *  the human string (used as the Error message). Not a transport failure —
 *  fetch/network errors reject with their own Error. */
export class CodeApiError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message || code);
    this.name = "CodeApiError";
    this.code = code;
  }
}

/** Shared failure fields of the `{ ok:false, code, message }` envelope. */
type CodeFail = { ok: boolean; code?: string; message?: string };

function codeQuery(
  projectSlug: string,
  sessionId: string,
  extra: Record<string, string> = {},
): string {
  const q = new URLSearchParams({
    project_slug: projectSlug,
    session_id: sessionId,
    ...extra,
  });
  return q.toString();
}

/** List the workspace roots for a session (primary folder first, then the
 *  other mounts, then the session workspace). */
export async function listCodeRoots(projectSlug: string, sessionId: string): Promise<CodeRoot[]> {
  const r = await json<CodeFail & { roots?: CodeRoot[] }>(
    `${BASE}/code/roots?${codeQuery(projectSlug, sessionId)}`,
  );
  if (!r.ok || !r.roots) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return r.roots;
}

/** List one directory level under `root` (lazy tree: one layer per call).
 *  `path` is root-relative; `""` is the root itself. */
export async function listCodeDir(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
): Promise<CodeListing> {
  const r = await json<
    CodeFail & { root?: string; path?: string; entries?: CodeEntry[]; truncated?: boolean }
  >(`${BASE}/code/list?${codeQuery(projectSlug, sessionId, { root, path })}`);
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return {
    root: r.root ?? root,
    path: r.path ?? path,
    entries: r.entries ?? [],
    truncated: !!r.truncated,
  };
}

/** Read a file's text. `rev` selects the HEAD blob instead of the worktree
 *  (used later for diffing). Degradations (binary / too-large / ro mount) come
 *  back as `ok:true` with `editable:false` + a `readonly_reason`, not an error;
 *  only a real refusal (denied path, outside root, …) throws. */
export async function readCodeFile(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  rev?: "worktree" | "HEAD",
): Promise<CodeRead> {
  const extra: Record<string, string> = { root, path };
  if (rev) extra.rev = rev;
  const r = await json<CodeFail & Partial<CodeRead>>(
    `${BASE}/code/read?${codeQuery(projectSlug, sessionId, extra)}`,
  );
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return {
    root: r.root ?? root,
    path: r.path ?? path,
    version: r.version ?? "",
    encoding: r.encoding ?? "utf-8",
    language: r.language ?? "plaintext",
    editable: !!r.editable,
    readonly_reason: r.readonly_reason ?? null,
    text: r.text ?? null,
    eof: r.eof !== false,
  };
}

/** URL of a file's raw bytes — for `<img src>`, which cannot use the JSON
 *  clients above (a JSON error body would render as a corrupt image, which is
 *  why the raw endpoint answers failures with a real HTTP status instead).
 *
 *  Runs the same fence as `readCodeFile`. `version` cache-busts the URL so a
 *  changed picture is re-fetched — that is what lets the server mark the
 *  response immutable. */
export function codeRawUrl(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  version?: string,
): string {
  const extra: Record<string, string> = { root, path };
  if (version) extra.v = version;
  return `${BASE}/code/raw?${codeQuery(projectSlug, sessionId, extra)}`;
}

/** URL of a workspace file shaped as PATH SEGMENTS instead of a query string —
 *  the HTML preview iframe loads the page from here, and its RELATIVE
 *  sub-resources (`./style.css`, `js/app.js`) resolve against the document
 *  URL's path (the query is dropped), which is exactly why this cannot be a
 *  `?path=` query like `codeRawUrl`. Same fence as `readCodeFile`; the sandbox
 *  headers live server-side (code.py `_CSP_TMPL`). `version` cache-busts the
 *  ENTRY document only — sub-resources revalidate via no-cache. */
export function codeFileUrl(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  version?: string,
): string {
  const seg = encodeURIComponent;
  const rel = path
    .split("/")
    .filter(Boolean)
    .map(seg)
    .join("/");
  const v = version ? `?v=${seg(version)}` : "";
  return `${BASE}/code/file/${seg(projectSlug)}/${seg(sessionId)}/${seg(root)}/${rel}${v}`;
}

/** A save was refused because the file changed on disk underneath it (409).
 *
 *  Carries the version currently ON DISK, so the caller can diff against it or
 *  re-save with the right baseline without a second round trip. */
export class CodeConflictError extends Error {
  readonly version: string;
  constructor(version: string, message: string) {
    super(message);
    this.name = "CodeConflictError";
    this.version = version;
  }
}

/** Save an open file.
 *
 *  `baseVersion` is the version the buffer was based on. The server compares it
 *  against disk and refuses (409) rather than silently overwriting a change it
 *  did not make — the "announce, never auto-overwrite" rule (design §6).
 *
 *  `encoding` must be the one the file was READ with: writing a GBK file back
 *  as UTF-8 would corrupt it silently.
 *
 *  Returns the file's new version. Throws `CodeConflictError` on 409, and
 *  `CodeApiError` for any other refusal. */
export async function writeCodeFile(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  args: { content: string; baseVersion: string; encoding?: string },
): Promise<string> {
  const r = await json<CodeFail & { version?: string }>(`${BASE}/code/write`, {
    method: "PUT",
    headers: H,
    body: JSON.stringify({
      project_slug: projectSlug,
      session_id: sessionId,
      root,
      path,
      content: args.content,
      base_version: args.baseVersion,
      encoding: args.encoding ?? "utf-8",
    }),
  });
  if (r.ok && r.version) return r.version;
  if (r.code === "conflict") {
    // 409 carries the on-disk version (see the brief §3.1).
    throw new CodeConflictError(r.version ?? "", r.message ?? "File has been modified on disk");
  }
  throw new CodeApiError(r.code ?? "not-text", r.message ?? "Save failed");
}

/** Git status for one root.
 *
 *  "Not a repo" is a NORMAL answer (`is_repo: false`), not an error: the tree
 *  simply shows no decorations. Only a real refusal (unknown root, ghost
 *  session) throws. */
export async function getCodeGit(
  projectSlug: string,
  sessionId: string,
  root: string,
): Promise<CodeGitStatus> {
  const r = await json<CodeFail & Partial<CodeGitStatus>>(
    `${BASE}/code/git?${codeQuery(projectSlug, sessionId, { root })}`,
  );
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
  return {
    is_repo: !!r.is_repo,
    toplevel: r.toplevel ?? null,
    branch: r.branch ?? null,
    entries: r.entries ?? {},
  };
}

// ---- file operations for the code panel (S4) --------------------------------
//
// Same convention as the clients above: the runtime replies with the house
// `{ok, ...}` envelope (HTTP 200 even on failure) and a refusal becomes a
// thrown `CodeApiError` carrying the server's own code and Chinese message,
// which the tree shows inline. `searchCode` is the one exception — its
// consumer contract is "always resolve", see its own comment.

/** Raise a refusal (`ok:false`) as a `CodeApiError`. */
function assertOk(r: CodeFail): void {
  if (!r.ok) throw new CodeApiError(r.code ?? "unknown-root", r.message ?? "");
}

/** Create a file or folder at the FULL root-relative `path` (parent + name).
 *  `kind:"file"` makes an empty file; the write endpoint cannot create one. */
export async function codeMkdir(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  kind: "file" | "dir",
): Promise<void> {
  const r = await json<CodeFail>(`${BASE}/code/mkdir`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({
      project_slug: projectSlug,
      session_id: sessionId,
      root,
      path,
      kind,
    }),
  });
  assertOk(r);
}

/** Rename one entry in place. `to` is the FULL root-relative destination path
 *  (parent + new name) — the server never re-uses the source's parent, so a
 *  bare name would land at the wrong place. */
export async function codeRename(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  to: string,
): Promise<void> {
  const r = await json<CodeFail>(`${BASE}/code/rename`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug: projectSlug, session_id: sessionId, root, path, to }),
  });
  assertOk(r);
}

/** Move one entry within its root (drag & drop). `to` is the full
 *  root-relative destination path; cross-root moves are out of scope. */
export async function codeMove(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  to: string,
): Promise<void> {
  const r = await json<CodeFail>(`${BASE}/code/move`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ project_slug: projectSlug, session_id: sessionId, root, path, to }),
  });
  assertOk(r);
}

export interface CodeDeleteResult {
  /** Immediate child count (0 for a file or an empty dir). In the `check_only`
   *  form this is the authoritative number the delete confirmation reports. */
  count: number;
  /** Server copy — the browser fallback explains it moved to the session
   *  `.trash-*` dir rather than the OS trash. */
  message?: string;
  trashPath?: string;
}

/** Delete (trash) one entry — never a permanent unlink.
 *
 *  `checkOnly` runs the server's write fence and returns the child count
 *  WITHOUT deleting. That is what lets the Tauri trash path stay safe: the
 *  Rust `code_trash` command only re-checks path containment, and
 *  `mount_access` is most-specific-match, so an `ro` mount nested inside a
 *  writable root is invisible when judged from the root alone — only the
 *  server's fence sees it (see `api/code_fsops.py`). */
export async function codeDelete(
  projectSlug: string,
  sessionId: string,
  root: string,
  path: string,
  opts: { confirm?: boolean; confirmCount?: number; checkOnly?: boolean } = {},
): Promise<CodeDeleteResult> {
  const body: Record<string, unknown> = {
    project_slug: projectSlug,
    session_id: sessionId,
    root,
    path,
  };
  if (opts.confirm) body.confirm = true;
  if (opts.confirmCount != null) body.confirm_count = opts.confirmCount;
  if (opts.checkOnly) body.check_only = true;
  const r = await json<CodeFail & { count?: number; trash_path?: string }>(`${BASE}/code/delete`, {
    method: "POST",
    headers: H,
    body: JSON.stringify(body),
  });
  assertOk(r);
  return { count: r.count ?? 0, message: r.message, trashPath: r.trash_path };
}

/** Search one root by name (⌘P) or content (⇧⌘F).
 *
 *  Unlike every other client here a refusal does NOT throw: it resolves with
 *  `error: {code, message}` so the injected `CodeSearchFn` stays "always
 *  resolve" and both views render the refusal in place (brief §3.4). The only
 *  rename against the wire is `elapsed_ms` → `elapsedMs`. */
export async function searchCode(
  projectSlug: string,
  sessionId: string,
  root: string,
  q: string,
  mode: CodeSearchMode,
  limit?: number,
): Promise<CodeSearchResult> {
  const extra: Record<string, string> = { root, q, mode };
  if (limit != null) extra.limit = String(limit);
  const r = await json<
    CodeFail & {
      hits?: CodeSearchHit[];
      scanned?: number;
      truncated?: boolean;
      elapsed_ms?: number;
    }
  >(`${BASE}/code/search?${codeQuery(projectSlug, sessionId, extra)}`);
  if (!r.ok) {
    return {
      hits: [],
      scanned: 0,
      truncated: false,
      elapsedMs: 0,
      error: { code: r.code ?? "unknown", message: r.message },
    };
  }
  return {
    hits: r.hits ?? [],
    scanned: r.scanned ?? 0,
    truncated: !!r.truncated,
    elapsedMs: r.elapsed_ms ?? 0,
  };
}
