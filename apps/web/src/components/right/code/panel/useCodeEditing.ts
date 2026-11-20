"use client";

/**
 * useCodeEditing — the SAVE/CONFLICT domain of the code panel (hooks split
 * 批次 3-3), a sub-domain of the tab state machine.
 *
 * Owns the S2 write path and every resolution of a refused save:
 *   - `saveTab` (⌘S): write against `base_version`; a refusal becomes the
 *     conflict bar (公告而不覆盖, design §6), NEVER an automatic overwrite.
 *   - `acceptSave`: adopt the new version as the clean baseline (bumps
 *     `savedNonce`, not `rev` — the buffer already equals disk).
 *   - `reloadTab` / `manualReload`(⌘R button): drop the buffer, take disk;
 *     the manual path confirms first when edits/conflict would be lost (issue #8).
 *   - `openDiff` (「看差异」): fetch the disk copy on demand, then show ConflictDiff.
 *   - `forceOverwrite` (「强制覆盖」): re-send the LIVE buffer (latestTextRef)
 *     against the version the 409 reported; a second conflict re-announces.
 *   - `htmlPreview` / `toggleHtmlPreview`: the per-tab 预览/源码 view mode.
 *
 * State lives in useCodeTabs; this hook only receives setters/refs, so the
 * two stay wired to the same tab bookkeeping.
 */

import { useCallback } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import {
  CodeApiError,
  CodeConflictError,
  readCodeFile,
  writeCodeFile,
} from "@/lib/runtime";
import { isRenderableHtml } from "../language";
import { codeTabKey, type EditorTab } from "../EditorTabs";
import { reloadNeedsConfirm } from "../reloadGuard";
import type { PanelT } from "./useCodeRoots";
import { PROJECT_SLUG, type ReadState, type TabMeta } from "./panelShared";

export interface UseCodeEditingOpts {
  sessionId: string | null;
  t: PanelT;
  setNotice: (s: string | null) => void;
  activeKey: string | null;
  activeTab: EditorTab | null;
  activeMeta: TabMeta | undefined;
  // ---- injected from useCodeTabs ----
  tabsRef: MutableRefObject<EditorTab[]>;
  tabMetaRef: MutableRefObject<Record<string, TabMeta>>;
  readsRef: MutableRefObject<Record<string, ReadState>>;
  patchMeta: (key: string, patch: (m: TabMeta) => Partial<TabMeta>) => void;
  dropRead: (key: string) => void;
  latestTextRef: MutableRefObject<Record<string, string>>;
  setReads: Dispatch<SetStateAction<Record<string, ReadState>>>;
  setReadNonce: Dispatch<SetStateAction<number>>;
  setShowDiff: Dispatch<SetStateAction<boolean>>;
  setInlineDiff: Dispatch<
    SetStateAction<{
      key: string;
      base: string;
      current: string;
      baseVersion: string;
      currentVersion: string;
      degraded: boolean;
    } | null>
  >;
}

export function useCodeEditing(opts: UseCodeEditingOpts) {
  const {
    sessionId,
    t,
    setNotice,
    activeKey,
    activeTab,
    activeMeta,
    tabsRef,
    tabMetaRef,
    readsRef,
    patchMeta,
    dropRead,
    latestTextRef,
    setReads,
    setReadNonce,
    setShowDiff,
    setInlineDiff,
  } = opts;

  // HTML 预览模式：meta.preview 未设置 = 自动（html/htm 直接出渲染页,其余源码），
  // 切换按钮写入显式值。渲染页读盘上字节,脏缓冲不参与（切回源码编辑,存盘后
  // version 变化会自动刷新 iframe）。
  const htmlPreview =
    !!activeTab && isRenderableHtml(activeTab.path) && (activeMeta?.preview ?? true);
  const toggleHtmlPreview = useCallback(() => {
    if (activeKey) patchMeta(activeKey, (m) => ({ preview: !(m.preview ?? true) }));
  }, [activeKey, patchMeta]);

  /**
   * Shared tail of a successful save: adopt the new version as the baseline.
   *
   * Bumps `savedNonce`, NOT `rev`: the buffer already equals what is on disk,
   * so rebuilding the model would only cost the cursor and the undo history.
   */
  const acceptSave = useCallback(
    (key: string, version: string) => {
      setReads((prev) => {
        const cur = prev[key];
        if (!cur?.data) return prev;
        return { ...prev, [key]: { ...cur, data: { ...cur.data, version } } };
      });
      patchMeta(key, (m) => ({
        ...m,
        dirty: false,
        conflict: null,
        savedNonce: m.savedNonce + 1,
      }));
      setShowDiff(false);
      setInlineDiff((cur) => (cur?.key === key ? null : cur));
    },
    [patchMeta, setReads, setShowDiff, setInlineDiff],
  );

  /** ⌘S, surfaced by MonacoEditor. */
  const saveTab = useCallback(
    async (key: string, text: string) => {
      const tab = tabsRef.current.find((tab) => codeTabKey(tab) === key);
      const read = readsRef.current[key];
      if (!sessionId || !tab || read?.status !== "ok" || !read.data) return;
      // Same gate the editor used to be read-only: an ro mount, a binary, an
      // image or an over-cap file has nothing saveable.
      if (!read.data.editable) return;
      try {
        const version = await writeCodeFile(PROJECT_SLUG, sessionId, tab.rootId, tab.path, {
          content: text,
          baseVersion: read.data.version,
          // The encoding the file was READ with — writing a GBK file back as
          // UTF-8 would corrupt it silently (brief §1-3).
          encoding: read.data.encoding,
        });
        acceptSave(key, version);
        setNotice(t("notice.saved", { path: tab.path }));
      } catch (err) {
        if (err instanceof CodeConflictError) {
          // Announce, never overwrite (design §6). Fetch the disk copy now so
          // 「看差异」 already has both sides when the user clicks it.
          let diskText = "";
          try {
            const disk = await readCodeFile(PROJECT_SLUG, sessionId, tab.rootId, tab.path);
            diskText = disk.text ?? "";
          } catch {
            /* leave it empty — the bar still explains what happened */
          }
          patchMeta(key, (m) => ({
            ...m,
            conflict: { version: err.version, diskText, mine: text },
          }));
        } else {
          setNotice(err instanceof CodeApiError ? err.message || err.code : t("notice.saveFailed"));
        }
      }
    },
    [sessionId, acceptSave, patchMeta, t, setNotice, tabsRef, readsRef],
  );

  /**
   * 「重新加载」 — drop the buffer and take what is on disk.
   *
   * `rev` is bumped so the editor REBUILDS its model. That is load-bearing:
   * MonacoEditor only recreates a model when the model URI (rootId + path)
   * changes, so without the bump the panel would refetch and still show the
   * stale buffer.
   */
  const reloadTab = useCallback(
    (key: string, notice?: string) => {
      dropRead(key);
      patchMeta(key, (m) => ({ ...m, dirty: false, conflict: null, rev: m.rev + 1 }));
      setShowDiff(false);
      setInlineDiff((cur) => (cur?.key === key ? null : cur));
      setReadNonce((n) => n + 1);
      setNotice(notice ?? t("notice.reloaded"));
    },
    [dropRead, patchMeta, t, setNotice, setShowDiff, setInlineDiff, setReadNonce],
  );

  // 手动刷新入口的防呆（issue #8）：dirty/conflict tab 直接 reload 会静默
  // 丢弃未保存修改（或未决冲突），先 window.confirm（与诊断修复同一确认惯例），
  // 取消则不动缓冲区。自动重载路径（code.changed 跟随）不走这里，行为不变。
  const manualReload = useCallback(
    (key: string) => {
      const m = tabMetaRef.current[key];
      if (
        reloadNeedsConfirm(m) &&
        !window.confirm(t("notice.reloadDiscardConfirm"))
      ) {
        return;
      }
      reloadTab(key);
    },
    [reloadTab, t, tabMetaRef],
  );

  /**
   * 「看差异」. A pushed change (agent wrote the file) carries only a version, so
   * the disk copy may not be loaded yet — fetch it now rather than at conflict
   * time, when the user asked for it.
   */
  const openDiff = useCallback(async () => {
    const key = activeKey;
    if (!key) return;
    const tab = tabsRef.current.find((tab) => codeTabKey(tab) === key);
    const meta = tabMetaRef.current[key];
    if (!tab || !meta?.conflict || !sessionId) return;
    if (meta.conflict.diskText == null) {
      // Refuse rather than open a MISLEADING diff: an empty "disk" side would
      // read as "the file is empty" instead of "we could not read it".
      let diskText: string;
      try {
        diskText = (await readCodeFile(PROJECT_SLUG, sessionId, tab.rootId, tab.path)).text ?? "";
      } catch {
        setNotice(t("notice.diffUnavailable"));
        return;
      }
      patchMeta(key, (m) =>
        m.conflict ? { ...m, conflict: { ...m.conflict, diskText } } : {},
      );
    }
    setShowDiff(true);
  }, [activeKey, sessionId, patchMeta, t, setNotice, setShowDiff, tabsRef, tabMetaRef]);

  /**
   * 「强制覆盖」 — re-send the buffer against the version the 409 reported.
   *
   * A SECOND conflict is possible (someone wrote again meanwhile). Report it
   * and let the user choose again rather than looping.
   */
  const forceOverwrite = useCallback(
    async (key: string) => {
      const tab = tabsRef.current.find((tab) => codeTabKey(tab) === key);
      const meta = tabMetaRef.current[key];
      const read = readsRef.current[key];
      if (!sessionId || !tab || !meta?.conflict) return;
      try {
        // The LIVE buffer, not `conflict.mine`: the user may have kept typing
        // since the refused save, and overwriting with the older text would
        // discard those keystrokes without a word.
        const content = latestTextRef.current[key] ?? meta.conflict.mine;
        const version = await writeCodeFile(PROJECT_SLUG, sessionId, tab.rootId, tab.path, {
          content,
          baseVersion: meta.conflict.version,
          encoding: read?.data?.encoding,
        });
        acceptSave(key, version);
        setNotice(t("notice.overwrote", { path: tab.path }));
      } catch (err) {
        if (err instanceof CodeConflictError) {
          patchMeta(key, (m) =>
            m.conflict ? { ...m, conflict: { ...m.conflict, version: err.version } } : {},
          );
          setNotice(t("notice.conflictAgain"));
        } else {
          setNotice(t("notice.forceSaveFailed"));
        }
      }
    },
    // latestTextRef is a stable ref — the live buffer for 强制覆盖.
    [sessionId, acceptSave, patchMeta, t, setNotice, tabsRef, tabMetaRef, readsRef, latestTextRef],
  );

  return {
    htmlPreview,
    toggleHtmlPreview,
    acceptSave,
    saveTab,
    reloadTab,
    manualReload,
    openDiff,
    forceOverwrite,
  };
}
