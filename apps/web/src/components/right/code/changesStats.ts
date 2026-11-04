/**
 * Pure helpers for ChangesPanel (§A) — kept free of component/alias imports so
 * the vitest suite (which has no "@/..." path resolution) can test them.
 */

/** Approximate +add / -del line counts: strip the common leading and trailing
 *  lines, then the leftovers ARE the change. Interior edits count as a full
 *  replace (delete+add), which is how git renders most edits anyway. */
export function lineDiffCounts(base: string, current: string): { add: number; del: number } {
  if (base === current) return { add: 0, del: 0 };
  const a = base.split("\n");
  const b = current.split("\n");
  let pre = 0;
  const minLen = Math.min(a.length, b.length);
  while (pre < minLen && a[pre] === b[pre]) pre += 1;
  let suf = 0;
  while (
    suf < minLen - pre &&
    a[a.length - 1 - suf] === b[b.length - 1 - suf]
  )
    suf += 1;
  return { add: b.length - pre - suf, del: a.length - pre - suf };
}
