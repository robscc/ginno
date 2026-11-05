/**
 * Issue #8 guard: a manual reload of a dirty/conflicted tab silently discards
 * unsaved edits — the entry points must confirm first. Extracted as a pure
 * predicate so it stays unit-testable in the node environment (no DOM).
 */
export interface ReloadGuardMeta {
  dirty: boolean;
  conflict: unknown;
}

export function reloadNeedsConfirm(
  meta: ReloadGuardMeta | undefined | null,
): boolean {
  if (!meta) return false;
  return !!meta.dirty || meta.conflict != null;
}
