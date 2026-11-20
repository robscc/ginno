"use client";

/**
 * useGitStatus — the GIT domain of the code panel (hooks split 批次 3-3).
 *
 * Owns the git letter decorations for the ACTIVE root and the agent-change
 * marks derived from the store's pushed change map. "与刷新" is deliberate: a
 * `refreshNonce` bump (manual refresh, a file op, a mount change coming out of
 * useCodeRoots) and any agent change both stand in for a server-side cache.
 */

import { useEffect, useMemo, useState } from "react";
import { getCodeGit } from "@/lib/runtime";
import type { CodeTouch } from "@/lib/store/useCodePanel";
import { PROJECT_SLUG } from "./panelShared";

/** Map the store's ABSOLUTE-path change map onto root-relative marks for one
 *  root, so a tree row can look itself up without knowing about roots. */
export function agentTouchedMap(
  codeTouched: Record<string, CodeTouch>,
  rootId: string | null,
  splitRootPath: (abs: string) => { rootId: string; rel: string } | null,
): Record<string, "write" | "edit"> | null {
  if (!rootId) return null;
  const out: Record<string, "write" | "edit"> = {};
  for (const [abs, info] of Object.entries(codeTouched)) {
    const m = splitRootPath(abs);
    if (m && m.rootId === rootId && m.rel) out[m.rel] = info.op;
  }
  return out;
}

export interface UseGitStatusOpts {
  sessionId: string | null;
  /** The root whose tree is on screen (CodePanel's effectiveRootId). */
  rootId: string | null;
  /** Panel-wide refresh nonce (useCodeRoots.bumpRefresh). */
  refreshNonce: number;
  /** Store's last agent change event (any change refetches the status). */
  lastChange: { nonce: number; path: string } | null;
  codeTouched: Record<string, CodeTouch>;
  splitRootPath: (abs: string) => { rootId: string; rel: string } | null;
}

export function useGitStatus(opts: UseGitStatusOpts) {
  const { sessionId, rootId, refreshNonce, lastChange, codeTouched, splitRootPath } = opts;

  const [gitStatus, setGitStatus] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    if (!sessionId || !rootId) {
      setGitStatus(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const st = await getCodeGit(PROJECT_SLUG, sessionId, rootId);
        if (!cancelled) setGitStatus(st.entries);
      } catch {
        // Not a repo / no git / sidecar hiccup: no decorations, never an error
        // (S3 brief §1-3). Refetched on a manual refresh and on any agent
        // change, which is what stands in for a server-side cache.
        if (!cancelled) setGitStatus(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, rootId, refreshNonce, lastChange]);

  const agentTouched = useMemo(
    () => agentTouchedMap(codeTouched, rootId, splitRootPath),
    [codeTouched, rootId, splitRootPath],
  );

  return { gitStatus, agentTouched };
}
