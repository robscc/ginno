"use client";

/**
 * useCodeDiff — the INLINE-DIFF / AGENT-FOLLOW domain of the code panel
 * (hooks split 批次 3-3), the S3 half of the tab state machine.
 *
 * Owns:
 *   - the follow effect for pushed `code.changed` events: a CLEAN tab follows
 *     the file (inline diff via the 500ms coalescing scheduler, degraded to a
 *     plain reload when no usable baseline exists); a DIRTY one is announced
 *     and never overwritten (design §6), reusing the S2 conflict bar.
 *   - the InlineDiffScheduler instance (one per panel; fire handler lives in a
 *     ref so it always sees current sessionId/roots without re-arming timers).
 *   - `openDiffForPath`: the ChangesPanel 联动 (code-panel-diff-design.md §A).
 *
 * Diff-mode FLAGS (`inlineDiff`, `showDiff`) live in useCodeTabs — the tab
 * lifecycle clears them — so this hook only writes them through the injected
 * setters, and `cancelRef` lets useCodeTabs' closeTab drop a pending diff.
 */

import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { codeDiff } from "@/lib/runtime";
import type { CodeRoot } from "@/lib/codeTypes";
import { InlineDiffScheduler, type ScheduledChange } from "@/lib/inlineDiffScheduler";
import { codeTabKey, type EditorTab } from "../EditorTabs";
import type { PanelT } from "./useCodeRoots";
import { PROJECT_SLUG, type InlineDiffState, type ReadState, type TabMeta } from "./panelShared";

export interface UseCodeDiffOpts {
  sessionId: string | null;
  t: PanelT;
  setNotice: (s: string | null) => void;
  /** Store's last agent change event ({nonce, path, version?}). */
  lastChange: { nonce: number; path: string; version?: string } | null;
  roots: CodeRoot[];
  absPathOf: (tab: { rootId: string; path: string }) => string | null;
  // ---- injected from useCodeTabs ----
  tabsRef: MutableRefObject<EditorTab[]>;
  tabMetaRef: MutableRefObject<Record<string, TabMeta>>;
  latestTextRef: MutableRefObject<Record<string, string>>;
  patchMeta: (key: string, patch: (m: TabMeta) => Partial<TabMeta>) => void;
  setReads: Dispatch<SetStateAction<Record<string, ReadState>>>;
  setInlineDiff: Dispatch<SetStateAction<InlineDiffState | null>>;
  openFile: (rootId: string, path: string, line?: number | null) => void;
  /** Panel-built bridge so closeTab can cancel a pending diff (see file doc). */
  cancelRef: MutableRefObject<(abs: string) => void>;
  // ---- injected from useCodeEditing ----
  reloadTab: (key: string, notice?: string) => void;
}

export function useCodeDiff(opts: UseCodeDiffOpts) {
  const {
    sessionId,
    t,
    setNotice,
    lastChange,
    roots,
    absPathOf,
    tabsRef,
    tabMetaRef,
    latestTextRef,
    patchMeta,
    setReads,
    setInlineDiff,
    openFile,
    cancelRef,
    reloadTab,
  } = opts;

  // ---- inline diff (code-panel-diff-design.md §B) ---------------------------
  // One scheduler per panel, created once; the handler lives in a ref so it
  // always sees the current sessionId/roots without re-arming timers.
  // Declared BEFORE the follow effect that schedules into it.
  const inlineDiffSchedulerRef = useRef<InlineDiffScheduler<{ rootId: string }> | null>(null);
  useEffect(() => {
    inlineDiffSchedulerRef.current = new InlineDiffScheduler<{ rootId: string }>(500, (ch) =>
      inlineDiffFireRef.current(ch),
    );
    cancelRef.current = (abs: string) => inlineDiffSchedulerRef.current?.cancel(abs);
    return () => {
      inlineDiffSchedulerRef.current?.dispose();
      inlineDiffSchedulerRef.current = null;
      cancelRef.current = () => {};
    };
  }, [cancelRef]);

  // ---- follow agent changes (S3) --------------------------------------------
  // A pushed `code.changed` for an OPEN file: a CLEAN tab follows the file, a
  // DIRTY one is announced and never overwritten (design §6). Because the event
  // carries the post-write version, the dirty case reuses the S2 conflict bar
  // verbatim rather than inventing a second UI (S3 brief §0).
  const appliedCodeNonceRef = useRef(0);
  useEffect(() => {
    const last = lastChange;
    if (!last || !sessionId) return;
    // Process each event at most once. The effect also re-runs when `roots`
    // resolve, and re-applying an old change would reload a tab for nothing.
    if (last.nonce === appliedCodeNonceRef.current) return;
    appliedCodeNonceRef.current = last.nonce;

    const hit = tabsRef.current.find((tab) => absPathOf(tab) === last.path);
    if (!hit) return;
    const key = codeTabKey(hit);

    if (tabMetaRef.current[key]?.dirty) {
      // No version in the event means nothing to compare against — the save
      // path will surface the conflict when the user presses ⌘S.
      if (!last.version) return;
      patchMeta(key, (m) => ({
        ...m,
        conflict: {
          version: last.version!,
          diskText: m.conflict?.diskText ?? null,
          mine: latestTextRef.current[key] ?? m.conflict?.mine ?? "",
        },
      }));
      setNotice(t("notice.agentChangedDirty"));
    } else {
      // CLEAN tab → inline diff mode (code-panel-diff-design.md §B). The
      // 500ms scheduler coalesces bursts; each fire fetches the session
      // baseline diff and only THEN swaps the editor for the overlay. A file
      // with no usable baseline (never agent-written / degraded / binary)
      // falls back to the plain reload follow.
      inlineDiffSchedulerRef.current?.schedule(last.path, last.version ?? "0", {
        rootId: hit.rootId,
      });
    }
  }, [lastChange, sessionId, absPathOf, patchMeta, t, setNotice, tabsRef, tabMetaRef, latestTextRef]);

  const exitInlineDiff = useCallback(() => {
    setInlineDiff(null);
  }, [setInlineDiff]);

  const handleInlineDiffFire = useCallback(
    async (ch: ScheduledChange<{ rootId: string }>) => {
      if (!sessionId) return;
      const tab = tabsRef.current.find((tb) => absPathOf(tb) === ch.path);
      if (!tab) return;
      const key = codeTabKey(tab);
      // The user may have started typing inside the debounce window: a dirty
      // tab never auto-enters diff (design §8), the conflict path owns it.
      if (tabMetaRef.current[key]?.dirty) return;
      try {
        const d = await codeDiff(PROJECT_SLUG, sessionId, ch.data.rootId, tab.path);
        if (!d.baselineExists || d.degraded || d.baseContent == null || d.currentContent == null) {
          // No usable baseline (agent never wrote it this session / caps /
          // binary): keep the old "reload and show a notice" follow.
          reloadTab(key, t("notice.agentChangedReloaded"));
          return;
        }
        // Adopt the current content as the tab's clean buffer BEFORE showing
        // the overlay, so exiting the diff lands on a fresh editable editor
        // (bumping `rev` makes Monaco rebuild its model with the new text).
        const currentText = d.currentContent;
        setReads((prev) => {
          const cur = prev[key];
          if (!cur?.data) return prev;
          return {
            ...prev,
            [key]: {
              ...cur,
              data: { ...cur.data, text: currentText, version: d.currentVersion || cur.data.version },
            },
          };
        });
        patchMeta(key, (m) => ({ ...m, dirty: false, conflict: null, rev: m.rev + 1 }));
        setInlineDiff({
          key,
          base: d.baseContent,
          current: currentText,
          baseVersion: d.baseVersion,
          currentVersion: d.currentVersion,
          degraded: d.degraded,
        });
      } catch {
        reloadTab(key, t("notice.agentChangedReloaded"));
      }
    },
    [sessionId, absPathOf, patchMeta, reloadTab, t, tabsRef, tabMetaRef, setReads, setInlineDiff],
  );
  const inlineDiffFireRef = useRef<(ch: ScheduledChange<{ rootId: string }>) => void>(() => {});
  useEffect(() => {
    inlineDiffFireRef.current = (ch) => void handleInlineDiffFire(ch);
  }, [handleInlineDiffFire]);

  /**
   * ChangesPanel 联动（code-panel-diff-design.md §A）：点变更列表里的一行 →
   * 打开该文件并进入 inline diff。复用 S3 的 500ms 调度器：openFile 先落地
   * tab，500ms 后 handleInlineDiffFire 在 tabsRef 里能找到它并拉取基线 diff。
   * 没有可用基线时该 handler 自行降级为 reload + 提示，无需此处的额外分支。
   */
  const openDiffForPath = useCallback(
    (rootId: string, rel: string) => {
      openFile(rootId, rel);
      const base = roots.find((r) => r.id === rootId)?.path;
      if (!base) return;
      const abs = rel ? `${base}/${rel}` : base;
      inlineDiffSchedulerRef.current?.schedule(abs, "0", { rootId });
    },
    [openFile, roots],
  );

  return { exitInlineDiff, openDiffForPath };
}
