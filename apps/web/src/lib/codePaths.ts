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
