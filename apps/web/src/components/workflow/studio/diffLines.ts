"use client";

/**
 * Tiny unified-diff generator for client-side drafts (node edits, imported
 * DSLs). The server's `diffWorkflowVersions` only compares *saved* versions;
 * a draft that hasn't been committed yet has no server-side diff, so we build
 * the same text format here and hand it to the existing <DiffView>.
 *
 * Output format matches difflib's unified diff: `---`/`+++` headers, `@@`
 * hunk headers (1-based line numbers, `-1,0` style for empty sides), then
 * context / `-` / `+` lines.
 */
export function lineDiff(before: string, after: string, label = "dsl"): string {
  const a = before.split("\n");
  const b = after.split("\n");
  if (before === after) return "";

  // LCS table — inputs are a single node's JSON (tens of lines), so the
  // quadratic table is cheap and avoids a dependency.
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  type Op = { t: " " | "-" | "+"; ln: string; ai: number; bi: number };
  const ops: Op[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ t: " ", ln: a[i], ai: i + 1, bi: j + 1 });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      ops.push({ t: "-", ln: a[i], ai: i + 1, bi: j + 1 });
      i++;
    } else {
      ops.push({ t: "+", ln: b[j], ai: i + 1, bi: j + 1 });
      j++;
    }
  }
  while (i < n) ops.push({ t: "-", ln: a[i], ai: i + 1, bi: m + 1 }), i++;
  while (j < m) ops.push({ t: "+", ln: b[j], ai: n + 1, bi: j + 1 }), j++;

  // Group changed runs into hunks with CONTEXT lines of surrounding context.
  const CONTEXT = 2;
  const changed = ops.map((o) => o.t !== " ");
  const out: string[] = [`--- a/${label}`, `+++ b/${label}`];
  let k = 0;
  while (k < ops.length) {
    if (!changed[k]) {
      k++;
      continue;
    }
    let start = k;
    while (start > 0 && changed[start - 1]) start--;
    start = Math.max(0, start - CONTEXT);
    let end = k;
    while (end < ops.length && (changed[end] || (end + 1 < ops.length && changed[end + 1]))) end++;
    end = Math.min(ops.length - 1, end + CONTEXT);

    const slice = ops.slice(start, end + 1);
    const aStart = slice[0].ai;
    const bStart = slice[0].bi;
    const aCount = slice.filter((o) => o.t !== "+").length;
    const bCount = slice.filter((o) => o.t !== "-").length;
    out.push(`@@ -${aStart},${aCount} +${bStart},${bCount} @@`);
    for (const o of slice) out.push(o.t + o.ln);
    k = end + 1;
  }
  return out.join("\n");
}

/** Pretty JSON for a value, for use as a diff side. */
export function pretty(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}