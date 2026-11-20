"use client";

/**
 * useCodeTabs — the TAB-STATE-MACHINE domain of the code panel (hooks split
 * 批次 3-3).
 *
 * Owns everything about WHICH files are open and their per-tab editing bookkeeping:
 *   - `tabs` + `activeKey` (+ LRU recency, MAX_TABS eviction over CLEAN tabs only)
 *   - `tabMeta` per tab (dirty / conflict / model-rev `rev` / `savedNonce` / preview)
 *   - `reads` — the per-tab fetch cache backing the editor
 *   - `jump` — pending tree reveal / editor line-centre requests
 *   - `showDiff` + `inlineDiff` — the two diff-mode flags. They live HERE (not
 *     in useCodeDiff) because the tab lifecycle itself clears them: opening,
 *     closing, saving and typing all exit diff mode.
 *
 * The inline-diff SCHEDULER belongs to useCodeDiff; `cancelInlineDiff` is
 * injected so closeTab can drop a pending debounced diff without a circular
 * dependency between the two hooks.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CodeApiError, readCodeFile } from "@/lib/runtime";
import type { CodeOpenRequest } from "@/lib/store/useCodePanel";
import { codeTabKey, type EditorTab } from "../EditorTabs";
import type { PanelT } from "./useCodeRoots";
import {
  EMPTY_TAB_META,
  MAX_TABS,
  PROJECT_SLUG,
  type InlineDiffState,
  type Jump,
  type ReadState,
  type TabMeta,
} from "./panelShared";

/** The LRU eviction victim for a new tab: oldest-used CLEAN tab that is not
 *  the active one. Null = every tab is dirty → grow past the cap rather than
 *  throw away someone's unsaved edits (brief §1-6). Pure so it is testable. */
export function pickEvictionVictim(
  openKeys: string[],
  recency: string[],
  newKey: string,
  activeKey: string | null,
  tabMeta: Record<string, TabMeta>,
): string | null {
  return (
    recency.find(
      (k) => k !== newKey && k !== activeKey && openKeys.includes(k) && !tabMeta[k]?.dirty,
    ) ?? null
  );
}

/** Which tab becomes active after `key` closes: the neighbour at the same
 *  index in the remaining list (falls back to the last one). Pure so it is
 *  testable. */
export function nextActiveAfterClose(
  tabs: EditorTab[],
  key: string,
): { nextTabs: EditorTab[]; fallback: EditorTab | null } {
  const idx = tabs.findIndex((tab) => codeTabKey(tab) === key);
  const nextTabs = tabs.filter((tab) => codeTabKey(tab) !== key);
  return { nextTabs, fallback: nextTabs[Math.min(idx, nextTabs.length - 1)] ?? null };
}

export interface UseCodeTabsOpts {
  sessionId: string | null;
  t: PanelT;
  setNotice: (s: string | null) => void;
  /** Drop a pending debounced inline diff for a closing tab (useCodeDiff owns
   *  the scheduler; the panel wires this to absPathOf + scheduler.cancel). */
  cancelInlineDiff: (tab: { rootId: string; path: string }) => void;
  // ---- jump-from-chat entry (design §3.4 统一入口) ----
  codeOpenRequest: CodeOpenRequest | null;
  clearCodeOpenRequest: () => void;
  setCodeRootId: (id: string) => void;
}

export function useCodeTabs(opts: UseCodeTabsOpts) {
  const { sessionId, t, setNotice, cancelInlineDiff, codeOpenRequest, clearCodeOpenRequest, setCodeRootId } =
    opts;

  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [reads, setReads] = useState<Record<string, ReadState>>({});
  const [jump, setJump] = useState<Jump | null>(null);
  const [readNonce, setReadNonce] = useState(0);

  // ---- per-tab editing state (S2) -------------------------------------------
  // `dirty` is reported by Monaco's alternative-version compare — never a text
  // diff, which would call an undo-to-original "still dirty" (design §4.5).
  // `conflict` holds a REFUSED save plus everything needed to resolve it, so
  // neither button needs another round trip.
  // `rev` is the model revision. It is bumped ONLY when the panel deliberately
  // replaces the buffer (reload), because remounting the editor drops the
  // cursor and the undo history — so a successful save must NOT bump it (the
  // buffer already equals what is on disk).
  const [tabMeta, setTabMeta] = useState<Record<string, TabMeta>>({});
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  // Inline diff (code-panel-diff-design.md §B): a CLEAN tab the agent rewrote
  // shows base-vs-current through InlineDiffOverlay until the user exits.
  // Keyed to the tab key, so switching tabs away (and back) keeps the state.
  const [inlineDiff, setInlineDiff] = useState<InlineDiffState | null>(null);

  const tabMetaRef = useRef<Record<string, TabMeta>>({});
  useEffect(() => {
    tabMetaRef.current = tabMeta;
  }, [tabMeta]);
  // Mirror of the open-tab list: eviction inside openFile needs the current
  // list synchronously, and reading state there would be stale.
  const tabsRef = useRef<EditorTab[]>([]);
  useEffect(() => {
    tabsRef.current = tabs;
  }, [tabs]);
  // The live buffer per tab, mirroring what Monaco has. A ref, not state: it
  // changes on every keystroke and nothing renders from it. 「强制覆盖」 reads it
  // so a conflict resolved AFTER further typing writes the current text instead
  // of the stale text from the failed attempt (silent data loss otherwise).
  const latestTextRef = useRef<Record<string, string>>({});
  // Most-recently-used LAST. Kept apart from `tabs` so selecting a tab never
  // reorders the tab strip under the user's cursor.
  const recencyRef = useRef<string[]>([]);
  // Guards a read request per tab key (StrictMode double-invokes effects).
  const inflightRef = useRef<Set<string>>(new Set());
  // Reads mirror for the fetch effect without making it depend on the state.
  const readsRef = useRef(reads);
  readsRef.current = reads;
  const jumpNonceRef = useRef(0);

  const patchMeta = useCallback((key: string, patch: (m: TabMeta) => Partial<TabMeta>) => {
    setTabMeta((prev) => {
      const cur = prev[key] ?? EMPTY_TAB_META;
      return { ...prev, [key]: { ...cur, ...patch(cur) } };
    });
  }, []);

  const dropRead = useCallback((key: string) => {
    setReads((prev) => {
      if (!(key in prev)) return prev;
      const copy = { ...prev };
      delete copy[key];
      return copy;
    });
  }, []);

  const activeTab = useMemo(
    // 回调参数不用 `t` 命名，避免遮蔽外层 i18n 翻译函数（i18n-design.md §10.4 已知坑）。
    () => tabs.find((tab) => codeTabKey(tab) === activeKey) ?? null,
    [tabs, activeKey],
  );

  // ---- opening files --------------------------------------------------------
  const openFile = useCallback(
    (rootId: string, path: string, line?: number | null) => {
      if (!rootId || !path) return;
      const key = codeTabKey({ rootId, path });
      recencyRef.current = [...recencyRef.current.filter((k) => k !== key), key];

      // LRU eviction, computed OUTSIDE the state updater: dropRead is a second
      // setState, and side effects inside an updater double-fire under
      // StrictMode (see the artifactsListRef comment in store.tsx).
      const openKeys = tabsRef.current.map(codeTabKey);
      if (!openKeys.includes(key) && openKeys.length >= MAX_TABS) {
        const victim = pickEvictionVictim(openKeys, recencyRef.current, key, activeKey, tabMetaRef.current);
        if (victim) {
          setTabs((prev) => prev.filter((tab) => codeTabKey(tab) !== victim));
          recencyRef.current = recencyRef.current.filter((k) => k !== victim);
          dropRead(victim);
        }
        // No victim means every tab is dirty: grow past the cap rather than
        // throw away someone's unsaved edits (brief §1-6).
      }

      setTabs((prev) =>
        prev.some((tab) => codeTabKey(tab) === key) ? prev : [...prev, { rootId, path }],
      );
      setActiveKey(key);
      jumpNonceRef.current += 1;
      setJump({ rootId, path, line: line ?? null, nonce: jumpNonceRef.current });
      setNotice(null);
    },
    [activeKey, dropRead, setNotice],
  );

  const revealDir = useCallback((rootId: string, path: string) => {
    jumpNonceRef.current += 1;
    setJump({ rootId, path, line: null, nonce: jumpNonceRef.current });
  }, []);

  const closeTab = useCallback(
    (key: string) => {
      // Closing a dirty tab throws edits away, so the first click only warns —
      // the tab's own tooltip says the same thing up front (EditorTabs). Two
      // clicks rather than a modal: an OS dialog mid-edit is worse.
      if (tabMetaRef.current[key]?.dirty && pendingClose !== key) {
        setPendingClose(key);
        setNotice(t("notice.unsavedClose"));
        return;
      }
      setPendingClose(null);
      const { nextTabs, fallback } = nextActiveAfterClose(tabs, key);
      setTabs(nextTabs);
      if (activeKey === key) {
        setActiveKey(fallback ? codeTabKey(fallback) : null);
        setJump(null);
      }
      dropRead(key);
      setTabMeta((prev) => {
        if (!(key in prev)) return prev;
        const copy = { ...prev };
        delete copy[key];
        return copy;
      });
      recencyRef.current = recencyRef.current.filter((k) => k !== key);
      delete latestTextRef.current[key];
      setShowDiff(false);
      // Closing the tab exits diff mode (design §4.1) and drops any pending
      // debounced diff for the file.
      setInlineDiff((cur) => (cur?.key === key ? null : cur));
      const closedTab = tabs.find((tb) => codeTabKey(tb) === key);
      if (closedTab) cancelInlineDiff?.(closedTab);
    },
    [tabs, activeKey, pendingClose, dropRead, cancelInlineDiff, t, setNotice],
  );

  const selectTab = useCallback((key: string) => {
    setActiveKey(key);
    // A manual tab switch must not re-run a reveal aimed at another file.
    setJump(null);
  }, []);

  // Read the active file's contents on demand (cache per tab key).
  useEffect(() => {
    if (!sessionId || !activeTab) return;
    const key = codeTabKey(activeTab);
    if (inflightRef.current.has(key)) return;
    const cur = readsRef.current[key];
    if (cur && cur.status !== "error") return;
    inflightRef.current.add(key);
    let cancelled = false;
    setReads((prev) => ({ ...prev, [key]: { status: "loading" } }));
    void (async () => {
      try {
        const data = await readCodeFile(PROJECT_SLUG, sessionId, activeTab.rootId, activeTab.path);
        if (!cancelled) setReads((prev) => ({ ...prev, [key]: { status: "ok", data } }));
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof CodeApiError ? err.message || err.code : t("notice.readFailed");
          setReads((prev) => ({ ...prev, [key]: { status: "error", error: message } }));
        }
      } finally {
        inflightRef.current.delete(key);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, activeTab, readNonce, t]);

  // Consume a jump-from-chat request (design §3.4 统一入口). An absolute path
  // means the file is outside every workspace root — surface the §3.2 guidance
  // instead of pretending to open it.
  useEffect(() => {
    const req = codeOpenRequest;
    if (!req) return;
    if (req.path.startsWith("/")) {
      setNotice(t("notice.outsideRoot"));
    } else {
      setCodeRootId(req.rootId);
      openFile(req.rootId, req.path, req.line ?? null);
    }
    clearCodeOpenRequest();
  }, [codeOpenRequest, openFile, setCodeRootId, clearCodeOpenRequest, t, setNotice]);

  return {
    // state (for rendering)
    tabs,
    activeKey,
    activeTab,
    tabMeta,
    reads,
    jump,
    pendingClose,
    showDiff,
    inlineDiff,
    // setters the sibling hooks / panel need
    setReads,
    setJump,
    setShowDiff,
    setInlineDiff,
    setReadNonce,
    // refs the sibling hooks read synchronously
    tabMetaRef,
    tabsRef,
    latestTextRef,
    readsRef,
    // helpers
    patchMeta,
    dropRead,
    // actions
    openFile,
    revealDir,
    closeTab,
    selectTab,
  };
}
