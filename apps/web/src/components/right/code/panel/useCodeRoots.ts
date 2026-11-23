"use client";

/**
 * useCodeRoots — the ROOTS domain of the code panel (hooks split 批次 3-3).
 *
 * Owns: the roots list + loading flag, the panel-wide refresh nonce (a bump
 * re-lists roots, refetches git status and re-lists the tree), the
 * absolute ↔ rooted path mapping, the default-root selection, and the RootBar
 * callbacks that mount a folder / flip an ro mount to rw.
 *
 * The REMOTE-session guard (filtering the always-empty session workspace out
 * of the switcher) stays in CodePanel: it is a panel-level concern and is
 * pinned by remoteComposerGuards.test.tsx reading the panel's source text —
 * the panel computes `displayRoots` and hands it in here.
 */

import { useCallback, useEffect, useState } from "react";
import {
  createFolder,
  listCodeRoots,
  putSessionContext,
  updateFolder,
} from "@/lib/runtime";
import type { CodeRoot } from "@/lib/codeTypes";
import { splitRootPathIn } from "@/lib/codePaths";
import type { SessionMeta } from "@/lib/types";
import { PROJECT_SLUG, fsError } from "./panelShared";

/** Message helper with the panel's i18n shape (see CodePanel's `t`). */
export type PanelT = (key: string, values?: Record<string, string | number>) => string;

/** Absolute path of a rooted tab, or null when its root is unknown. */
export function absPathOfTab(
  roots: CodeRoot[],
  tab: { rootId: string; path: string },
): string | null {
  const base = roots.find((r) => r.id === tab.rootId)?.path;
  if (!base) return null;
  return tab.path ? `${base}/${tab.path}` : base;
}

/** The root the panel should select once roots arrive: the SESSION WORKSPACE
 *  first, else the first root. (Remote sessions: the workspace root is already
 *  filtered out of `displayRoots`, so this falls through to the first mount.)
 *  Used by the panel's default-root effect — see the file comment for why that
 *  effect lives in CodePanel rather than here. */
export function pickDefaultRoot(displayRoots: CodeRoot[]): CodeRoot | undefined {
  return displayRoots.find((r) => r.id === "session") ?? displayRoots[0];
}

export interface UseCodeRootsOpts {
  sessionId: string | null;
  sessions: SessionMeta[];
  applySessionPatch: (id: string, patch: Partial<SessionMeta>) => void;
  t: PanelT;
}

export function useCodeRoots(opts: UseCodeRootsOpts) {
  const { sessionId, sessions, applySessionPatch, t } = opts;

  const [roots, setRoots] = useState<CodeRoot[]>([]);
  const [rootsLoading, setRootsLoading] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  /** Panel-wide refresh: bumping re-lists roots (below), refetches git status
   *  (useGitStatus) and re-lists the tree (FileTree's refreshNonce prop). */
  const bumpRefresh = useCallback(() => setRefreshNonce((n) => n + 1), []);

  const loadRoots = useCallback(async () => {
    if (!sessionId) {
      setRoots([]);
      return;
    }
    setRootsLoading(true);
    try {
      setRoots(await listCodeRoots(PROJECT_SLUG, sessionId));
    } catch {
      setRoots([]);
    } finally {
      setRootsLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void loadRoots();
  }, [loadRoots, refreshNonce]);

  const splitRootPath = useCallback(
    (abs: string) => splitRootPathIn(roots, abs),
    [roots],
  );

  const absPathOf = useCallback(
    (tab: { rootId: string; path: string }) => absPathOfTab(roots, tab),
    [roots],
  );

  // ---- RootBar: register/mount a folder, flip an ro mount to rw ------------
  const onAddFolder = useCallback(
    async (path: string): Promise<string | null> => {
      if (!sessionId) return t("panel.noSession");
      try {
        const created = await createFolder({ path, access: "rw", load_rules: true });
        if (!created.ok || !created.folder) return created.error || t("fs.mountFailed");
        const folderId = created.folder.id;
        const meta = sessions.find((s) => s.id === sessionId);
        const ids = meta?.context_folders ?? [];
        const nextIds = ids.includes(folderId) ? [...ids] : [...ids, folderId];
        // Keep the session's existing primary (may be null): adding a mount must
        // NOT move the agent's relative-path base — that is a separate action.
        const primary = meta?.primary_folder ?? null;
        const r = await putSessionContext(sessionId, {
          folder_ids: nextIds,
          primary_id: primary,
        });
        if (!r.ok) return r.error || t("fs.mountFailed");
        // Keep the store's session in step so the chip and a later add see it.
        applySessionPatch(sessionId, {
          context_folders: nextIds,
          primary_folder: primary,
        });
        bumpRefresh(); // the new root must appear
        return null;
      } catch (err) {
        return fsError(err, t("fs.mountFailed"));
      }
    },
    [sessionId, sessions, applySessionPatch, t, bumpRefresh],
  );

  const onMakeRootWritable = useCallback(
    async (rootId: string): Promise<string | null> => {
      try {
        const r = await updateFolder(rootId, { access: "rw" });
        if (!r.ok) return r.error || t("fs.switchFailed");
        bumpRefresh(); // re-list roots so the badge flips
        return null;
      } catch (err) {
        return fsError(err, t("fs.switchFailed"));
      }
    },
    [t, bumpRefresh],
  );

  return {
    roots,
    rootsLoading,
    refreshNonce,
    bumpRefresh,
    splitRootPath,
    absPathOf,
    onAddFolder,
    onMakeRootWritable,
  };
}
