import type { CodeRoot } from "./codeTypes";

export type MarkdownFileTarget = { path: string; line?: number };

/** Chat file citations use an absolute path, optionally with a line suffix. */
export function parseMarkdownFileTarget(href: string | undefined): MarkdownFileTarget | null {
  if (!href || !href.startsWith("/") || href.startsWith("//") || /^\/(api|_next)\//.test(href)) {
    return null;
  }
  const match = /^(\/[^?#]+?)(?::([1-9]\d*)|#L([1-9]\d*))?$/.exec(href);
  if (!match) return null;
  try {
    const path = decodeURIComponent(match[1]);
    if (path.startsWith("//") || path.includes("\\") || path.includes("\0")) return null;
    const line = Number(match[2] ?? match[3]);
    return Number.isSafeInteger(line) ? { path, line } : { path };
  } catch {
    return null;
  }
}

/** Directories the one-click guidance never offers to mount: a file sitting
 *  directly in one of them names no project, and mounting it would hand the
 *  session a system-wide tree. */
const TOO_BROAD_TO_MOUNT = new Set([
  "/tmp", "/private/tmp", "/var", "/private/var", "/Volumes", "/Users/Shared",
]);

/** The folder the out-of-root guidance offers to add (design §3.2): the
 *  file's containing directory — or null when that directory is too broad to
 *  mount on one click (filesystem root, /Users, /tmp, …). */
export function folderToMountFor(absolute: string): string | null {
  if (!absolute.startsWith("/") || absolute.endsWith("/")) return null;
  const dir = absolute.slice(0, absolute.lastIndexOf("/"));
  if (dir.split("/").filter(Boolean).length < 2 || TOO_BROAD_TO_MOUNT.has(dir)) return null;
  return dir;
}

/** Pick the most specific mount before passing the relative path to /api/code. */
export function splitRootPathIn(roots: CodeRoot[], absolute: string): { rootId: string; rel: string } | null {
  let best: { rootId: string; rel: string } | null = null;
  for (const { id, path } of roots) {
    if (!path) continue;
    const prefix = path.endsWith("/") ? path : `${path}/`;
    if (absolute !== prefix.slice(0, -1) && !absolute.startsWith(prefix)) continue;
    const rel = absolute.slice(prefix.length);
    if (!best || rel.length < best.rel.length) best = { rootId: id, rel };
  }
  return best;
}
