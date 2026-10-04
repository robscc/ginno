"use client";

/**
 * Assembles the code panel from RootBar / FileTree / EditorTabs / MonacoEditor
 * (docs/code-panel-design.md §3.3, §3.4, §4.5).
 *
 * The panel has two layouts inside the one right-panel tab:
 *   - "file" (default): the tree collapses to a breadcrumb bar and the editor
 *     takes the full panel width. ⌘B slides the tree out as an overlay drawer
 *     from the left, so it never squeezes the editor.
 *   - "side": tree (260px) + editor side by side. Chosen automatically once the
 *     panel is ≥600px wide, or by the user via the layout toggle / ⌘B.
 * A user choice wins: once the mode is toggled by hand, resizing no longer
 * overrides it. Below 600px the panel is forced back to "file".
 *
 * Stage map (docs/code-panel-design.md §4.7) — this file is where they meet:
 *   S1  read + browse: files come from `/api/code/read`, the tree lists one level
 *       at a time, images render through `/api/code/raw` (ImagePreview).
 *   S2  write: ⌘S saves against a `base_version`; a refusal becomes a conflict
 *       bar (重新加载 / 看差异 / 强制覆盖) and NEVER an automatic overwrite.
 *   S3  agent changes + git: the socket's `code.changed` drives a clean tab to
 *       follow the file and a dirty one to the same conflict bar; git letters and
 *       agent marks come from `/api/code/git` and the pushed change map.
 *   S4  search + file ops: ⌘P / ⇧⌘F, and create/rename/move/delete through
 *       `/api/code/{mkdir,rename,move,delete}`. Delete runs the server's fence
 *       FIRST (`check_only`) and only then hands the entry to the OS trash — the
 *       Rust command cannot see mount tiers, so the server must be the gate.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  AlertTriangle,
  ChevronRight,
  Columns2,
  FolderTree,
  Info,
  RotateCcw,
  Save,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import {
  CodeApiError,
  CodeConflictError,
  codeDelete,
  codeMkdir,
  codeMove,
  codeRename,
  codeRawUrl,
  createFolder,
  getCodeGit,
  listCodeRoots,
  putSessionContext,
  readCodeFile,
  searchCode,
  updateFolder,
  writeCodeFile,
} from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import type { CodeRead, CodeRoot } from "@/lib/codeTypes";
import { cn } from "@/lib/utils";
import { RootBar } from "./RootBar";
import { FileTree } from "./FileTree";
import { EditorTabs, codeTabKey, type EditorTab } from "./EditorTabs";
import { MonacoEditor } from "./MonacoEditor";
import { ImagePreview } from "./ImagePreview";
import { ConflictDiff } from "./ConflictDiff";
import { QuickOpen } from "./QuickOpen";
import { SearchView, type CodeSearchFn } from "./SearchView";

/** Panel width at/above which the tree+editor "side" layout is allowed. */
const WIDE_PX = 600;
/** Fixed tree column width in "side" mode (design §3.3: 220–300px). */
const TREE_PX = 260;
/** The web app is single-project; same convention as listArtifacts/listSkills. */
const PROJECT_SLUG = "default";

/** Open-tab cap (design §4.7). Eviction is LRU over CLEAN tabs only. */
const MAX_TABS = 20;

/** Message for a failed file op. Prefer the server's own Chinese wording — it
 *  names the real reason ("当前文件夹以只读方式挂载", "已存在同名条目", …), which a
 *  generic fallback would throw away. */
function fsError(err: unknown, fallback: string): string {
  if (err instanceof CodeApiError) return err.message || err.code;
  return err instanceof Error ? err.message : fallback;
}

/** Escape user text so it matches LITERALLY inside the server's regex.
 *  Content search compiles `q` as a regex while the result highlight compares
 *  literal text (SearchView.Highlighted); leaving `foo.bar` unescaped would
 *  match `fooXbar` and highlight nothing, and `foo(` would fail with
 *  `invalid-regex`. Regex stays available at the API layer for a future toggle. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Editing state for one open tab (S2). */
interface TabMeta {
  /** Unsaved edits in the buffer. */
  dirty: boolean;
  /** A save was refused — or the agent changed the file underneath — and the
   *  panel must not overwrite it. A 409 fills both texts; a PUSHED change
   *  carries only a version, so `diskText` may be null and 「看差异」 fetches it
   *  on demand (S3 brief §0). */
  conflict: { version: string; diskText: string | null; mine: string } | null;
  /** Bumped to REBUILD the editor's model (see the comment in the component). */
  rev: number;
  /** Bumped to tell Monaco "the buffer is the new clean baseline". */
  savedNonce: number;
}

const EMPTY_TAB_META: TabMeta = { dirty: false, conflict: null, rev: 0, savedNonce: 0 };

interface ReadState {
  status: "loading" | "ok" | "error";
  data?: CodeRead;
  error?: string;
}

/** A pending reveal: which root/path to focus in the tree and, when jumping
 *  from chat, which line to centre in Monaco. */
interface Jump {
  rootId: string;
  path: string;
  line: number | null;
  nonce: number;
}

export function CodePanel() {
  const g = useGinno();
  const t = useTranslations("code");
  const sessionId = g.activeSessionId;
  const panelWidth = g.rightPanelWidth;
  const wide = panelWidth >= WIDE_PX;
  // Below 600px "side" is not allowed — the tree becomes a drawer instead.
  const effectiveMode: "file" | "side" = wide ? g.codePanelMode : "file";

  const {
    codeRootId,
    setCodeRootId,
    setCodePanelMode,
    setCodeTreeOpen,
    codeOpenRequest,
    clearCodeOpenRequest,
  } = g;

  const [roots, setRoots] = useState<CodeRoot[]>([]);
  const [rootsLoading, setRootsLoading] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [readNonce, setReadNonce] = useState(0);
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  // BROWSING = nothing open. The panel's job then is to show the tree: an empty
  // editor beside a collapsed tree is a dead pane, and since the default panel
  // (380px) is below WIDE_PX the auto-sizing below would otherwise open "file"
  // mode with the tree shut — i.e. the whole tab looking empty on entry.
  // Declared here, not with `effectiveMode`, because it reads `tabs`.
  const browsing = tabs.length === 0;
  const treeVisible = browsing || (effectiveMode === "side" && g.codeTreeOpen);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [reads, setReads] = useState<Record<string, ReadState>>({});
  const [jump, setJump] = useState<Jump | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Panel-level overlays owned by the keyboard layer below (⌘P / ⇧⌘F).
  const [quickOpen, setQuickOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);

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

  // ---- absolute ↔ rooted path mapping (S3) ----------------------------------
  // The socket and the git endpoint speak ABSOLUTE paths; the panel addresses
  // files as (rootId, root-relative path). Longest prefix wins, so a root
  // nested inside another resolves to the inner one rather than the outer.
  const splitRootPath = useCallback(
    (abs: string): { rootId: string; rel: string } | null => {
      let best: { rootId: string; rel: string; len: number } | null = null;
      for (const r of roots) {
        const base = r.path.endsWith("/") ? r.path.slice(0, -1) : r.path;
        if (!base) continue;
        if (abs === base || abs.startsWith(base + "/")) {
          if (!best || base.length > best.len) {
            best = { rootId: r.id, rel: abs.slice(base.length + 1), len: base.length };
          }
        }
      }
      return best ? { rootId: best.rootId, rel: best.rel } : null;
    },
    [roots],
  );

  const absPathOf = useCallback(
    (t: EditorTab): string | null => {
      const base = roots.find((r) => r.id === t.rootId)?.path;
      if (!base) return null;
      return t.path ? `${base}/${t.path}` : base;
    },
    [roots],
  );

  // Once the user picks a layout by hand, auto-sizing must not override it.
  const modeTouchedRef = useRef(false);
  // Guards a read request per tab key (StrictMode double-invokes effects).
  const inflightRef = useRef<Set<string>>(new Set());
  // Reads mirror for the fetch effect without making it depend on the state.
  const readsRef = useRef(reads);
  readsRef.current = reads;
  const jumpNonceRef = useRef(0);

  const effectiveRootId = codeRootId ?? roots[0]?.id ?? null;
  /** The root object behind `effectiveRootId` — file ops need its absolute
   *  `path` (the Tauri commands and the clipboard take absolute paths). */
  const activeRoot = useMemo(
    () => roots.find((r) => r.id === effectiveRootId) ?? null,
    [roots, effectiveRootId],
  );
  /** Absolute path of a root-relative entry in the ACTIVE root. */
  const absInActiveRoot = useCallback(
    (path: string): string | null => {
      if (!activeRoot) return null;
      return path ? `${activeRoot.path}/${path}` : activeRoot.path;
    },
    [activeRoot],
  );
  const activeTab = useMemo(
    // 回调参数不用 `t` 命名，避免遮蔽外层 i18n 翻译函数（i18n-design.md §10.4 已知坑）。
    () => tabs.find((tab) => codeTabKey(tab) === activeKey) ?? null,
    [tabs, activeKey],
  );
  const activeRead = activeKey ? reads[activeKey] : undefined;
  const activeMeta = activeKey ? tabMeta[activeKey] : undefined;

  // ---- git status for the active root (S3) ----------------------------------
  const [gitStatus, setGitStatus] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    if (!sessionId || !effectiveRootId) {
      setGitStatus(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const st = await getCodeGit(PROJECT_SLUG, sessionId, effectiveRootId);
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
  }, [sessionId, effectiveRootId, refreshNonce, g.codeLastChange]);

  /** Agent marks for the ACTIVE root, keyed root-relative so a tree row can
   *  look itself up without knowing about roots. */
  const agentTouched = useMemo(() => {
    if (!effectiveRootId) return null;
    const out: Record<string, "write" | "edit"> = {};
    for (const [abs, info] of Object.entries(g.codeTouched)) {
      const m = splitRootPath(abs);
      if (m && m.rootId === effectiveRootId && m.rel) out[m.rel] = info.op;
    }
    return out;
  }, [g.codeTouched, effectiveRootId, splitRootPath]);

  // ---- roots ----------------------------------------------------------------
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

  // Pick a root once roots arrive (primary folder first is the server's order).
  useEffect(() => {
    if (codeRootId || !roots.length) return;
    // Default to the SESSION WORKSPACE rather than `roots[0]`: the server orders
    // mounts first, so `roots[0]` is a mounted project folder when one exists —
    // but the session workspace always exists and is where the agent's own
    // output lands, so it is never an empty pane. A project folder is one click
    // away in the switcher.
    const preferred = roots.find((r) => r.id === "session") ?? roots[0];
    setCodeRootId(preferred.id);
  }, [codeRootId, roots, setCodeRootId]);

  // ---- layout auto-sizing ---------------------------------------------------
  useEffect(() => {
    if (modeTouchedRef.current) return;
    const next: "file" | "side" = wide ? "side" : "file";
    setCodePanelMode(next);
    setCodeTreeOpen(wide);
  }, [wide, setCodePanelMode, setCodeTreeOpen]);

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
        const victim = recencyRef.current.find(
          (k) =>
            k !== key &&
            k !== activeKey &&
            openKeys.includes(k) &&
            !tabMetaRef.current[k]?.dirty,
        );
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
    [activeKey, dropRead],
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
      const idx = tabs.findIndex((tab) => codeTabKey(tab) === key);
      const next = tabs.filter((tab) => codeTabKey(tab) !== key);
      setTabs(next);
      if (activeKey === key) {
        const fallback = next[Math.min(idx, next.length - 1)];
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
    },
    [tabs, activeKey, pendingClose, dropRead, t],
  );

  const selectTab = useCallback((key: string) => {
    setActiveKey(key);
    // A manual tab switch must not re-run a reveal aimed at another file.
    setJump(null);
  }, []);

  // ---- saving (S2) ----------------------------------------------------------
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
    },
    [patchMeta],
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
    [sessionId, acceptSave, patchMeta, t],
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
      setReadNonce((n) => n + 1);
      setNotice(notice ?? t("notice.reloaded"));
    },
    [dropRead, patchMeta, t],
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
  }, [activeKey, sessionId, patchMeta, t]);

  // ---- follow agent changes (S3) --------------------------------------------
  // A pushed `code.changed` for an OPEN file: a CLEAN tab follows the file, a
  // DIRTY one is announced and never overwritten (design §6). Because the event
  // carries the post-write version, the dirty case reuses the S2 conflict bar
  // verbatim rather than inventing a second UI (S3 brief §0).
  const appliedCodeNonceRef = useRef(0);
  useEffect(() => {
    const last = g.codeLastChange;
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
          version: last.version,
          diskText: m.conflict?.diskText ?? null,
          mine: latestTextRef.current[key] ?? m.conflict?.mine ?? "",
        },
      }));
      setNotice(t("notice.agentChangedDirty"));
    } else {
      reloadTab(key, t("notice.agentChangedReloaded"));
    }
  }, [g.codeLastChange, sessionId, absPathOf, patchMeta, reloadTab, t]);

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
    [sessionId, acceptSave, patchMeta, t],
  );

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
  }, [codeOpenRequest, openFile, setCodeRootId, clearCodeOpenRequest, t]);

  // ---- manual controls ------------------------------------------------------
  const toggleTree = useCallback(() => {
    const opening = !g.codeTreeOpen;
    setCodeTreeOpen(opening);
    // On a wide panel, the manual way into "side" is opening the tree.
    if (opening && wide && effectiveMode === "file") {
      modeTouchedRef.current = true;
      setCodePanelMode("side");
    }
  }, [g.codeTreeOpen, wide, effectiveMode, setCodeTreeOpen, setCodePanelMode]);

  const toggleMode = useCallback(() => {
    modeTouchedRef.current = true;
    const next: "file" | "side" = effectiveMode === "side" ? "file" : "side";
    setCodePanelMode(next);
    setCodeTreeOpen(next === "side");
  }, [effectiveMode, setCodePanelMode, setCodeTreeOpen]);

  // ⌘B toggles the tree for as long as the code tab is mounted (design §3.4).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === "b" || e.key === "B")) {
        e.preventDefault();
        toggleTree();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleTree]);

  const onRefresh = useCallback(() => {
    setRefreshNonce((n) => n + 1);
    if (activeKey) {
      setReads((prev) => {
        if (!(activeKey in prev)) return prev;
        const copy = { ...prev };
        delete copy[activeKey];
        return copy;
      });
      setReadNonce((n) => n + 1);
    }
  }, [activeKey]);

  const selectRoot = useCallback(
    (id: string) => {
      setCodeRootId(id);
      setJump(null);
    },
    [setCodeRootId],
  );

  // ---- S4 file operations: the panel owns every request ----------------------
  // FileTree only renders UI; each callback resolves to a Chinese error message
  // (shown inline by the tree) or `null` on success. The tree re-lists the
  // affected directory itself after a create / rename / move, so those paths
  // must NOT bump `refreshNonce` — that would reset (collapse) the whole tree.

  /** Create directly under `dir` (`""` = root). `name` is one component. */
  const onTreeCreate = useCallback(
    async (dir: string, name: string, kind: "file" | "dir"): Promise<string | null> => {
      if (!sessionId || !effectiveRootId) return t("panel.noWorkspace");
      const path = dir ? `${dir}/${name}` : name;
      try {
        await codeMkdir(PROJECT_SLUG, sessionId, effectiveRootId, path, kind);
        return null;
      } catch (err) {
        return fsError(err, t("fs.createFailed"));
      }
    },
    [sessionId, effectiveRootId, t],
  );

  /** Rename in place. FileTree hands over a BARE new name; the endpoint wants
   *  the full root-relative `to`, so re-attach the source's parent here. */
  const onTreeRename = useCallback(
    async (path: string, name: string): Promise<string | null> => {
      if (!sessionId || !effectiveRootId) return t("panel.noWorkspace");
      const cut = path.lastIndexOf("/");
      const to = cut < 0 ? name : `${path.slice(0, cut)}/${name}`;
      try {
        await codeRename(PROJECT_SLUG, sessionId, effectiveRootId, path, to);
        return null;
      } catch (err) {
        return fsError(err, t("fs.renameFailed"));
      }
    },
    [sessionId, effectiveRootId, t],
  );

  /** Drag & drop move. `to` is already the full root-relative destination. */
  const onTreeMove = useCallback(
    async (from: string, to: string): Promise<string | null> => {
      if (!sessionId || !effectiveRootId) return t("panel.noWorkspace");
      try {
        await codeMove(PROJECT_SLUG, sessionId, effectiveRootId, from, to);
        return null;
      } catch (err) {
        return fsError(err, t("fs.moveFailed"));
      }
    },
    [sessionId, effectiveRootId, t],
  );

  /** Delete one entry.
   *
   *  Order is load-bearing. First the SERVER fence (`check_only`) — it is the
   *  only component that knows about mounts, and `mount_access` is
   *  most-specific-match, so an `ro` mount nested inside a writable root looks
   *  writable from the root alone. The Rust `code_trash` command below only
   *  re-checks path containment and CANNOT see that; a refusal here must stop
   *  the flow outright (never fall through to the trash). Then, on desktop, the
   *  OS trash via Tauri; in the browser, the server's `.trash-*` fallback. */
  const onTreeDelete = useCallback(
    async (path: string): Promise<string | null> => {
      if (!sessionId || !effectiveRootId || !activeRoot) return t("panel.noWorkspace");
      let count: number;
      try {
        ({ count } = await codeDelete(PROJECT_SLUG, sessionId, effectiveRootId, path, {
          checkOnly: true,
        }));
      } catch (err) {
        return fsError(err, t("fs.deleteFailed")); // fence refused — do NOT continue
      }
      const abs = absInActiveRoot(path);
      try {
        if (isDesktop()) {
          if (!abs) return t("panel.noWorkspace");
          await invoke("code_trash", { root: activeRoot.path, path: abs });
        } else {
          await codeDelete(PROJECT_SLUG, sessionId, effectiveRootId, path, {
            confirm: true,
            confirmCount: count,
          });
        }
      } catch (err) {
        return fsError(err, t("fs.deleteFailed"));
      }
      setRefreshNonce((n) => n + 1);
      return null;
    },
    [sessionId, effectiveRootId, activeRoot, absInActiveRoot, t],
  );

  const onCopyPath = useCallback(
    (path: string, absolute: boolean) => {
      const text = absolute ? absInActiveRoot(path) ?? path : path;
      void navigator.clipboard
        ?.writeText(text)
        .then(() => setNotice(t("notice.copied", { path: text })))
        .catch(() => setNotice(t("notice.copyFailed")));
    },
    [absInActiveRoot, t],
  );

  // Reveal / open-external are Tauri-only; invoking them in a plain browser
  // throws, so gate on `isDesktop()` and pass ABSOLUTE paths (the shell
  // re-checks containment against the root it is given).
  const onTreeReveal = useCallback(
    (path: string) => {
      if (!isDesktop() || !activeRoot) return;
      const abs = absInActiveRoot(path);
      if (!abs) return;
      invoke("code_reveal", { root: activeRoot.path, path: abs }).catch((err) =>
        setNotice(typeof err === "string" ? err : t("notice.revealFailed")),
      );
    },
    [activeRoot, absInActiveRoot, t],
  );

  const onTreeOpenExternal = useCallback(
    (path: string) => {
      if (!isDesktop() || !activeRoot) return;
      const abs = absInActiveRoot(path);
      if (!abs) return;
      invoke("code_open_external", { root: activeRoot.path, path: abs }).catch((err) =>
        setNotice(typeof err === "string" ? err : t("notice.openExternalFailed")),
      );
    },
    [activeRoot, absInActiveRoot, t],
  );

  // ---- search injection (⌘P / ⇧⌘F) ------------------------------------------
  const search: CodeSearchFn = useCallback(
    async (mode, q, limit) => {
      if (!sessionId || !effectiveRootId) {
        return {
          hits: [],
          scanned: 0,
          truncated: false,
          elapsedMs: 0,
          error: { code: "unknown-root", message: t("panel.noWorkspace") },
        };
      }
      // name → literal (the server matches case-insensitively); content → the
      // server compiles a REGEX, so escape the text into a literal and ask for
      // case-insensitivity, matching what the view highlights. Regex power
      // stays at the API layer for a future explicit toggle.
      const query = mode === "content" ? "(?i)" + escapeRegExp(q) : q;
      return searchCode(PROJECT_SLUG, sessionId, effectiveRootId, query, mode, limit);
    },
    [sessionId, effectiveRootId, t],
  );

  // ---- RootBar: register/mount a folder, flip an ro mount to rw -------------
  const onAddFolder = useCallback(
    async (path: string): Promise<string | null> => {
      if (!sessionId) return t("panel.noSession");
      try {
        const created = await createFolder({ path, access: "rw", load_rules: true });
        if (!created.ok || !created.folder) return created.error || t("fs.mountFailed");
        const folderId = created.folder.id;
        const meta = g.sessions.find((s) => s.id === sessionId);
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
        g.applySessionPatch(sessionId, {
          context_folders: nextIds,
          primary_folder: primary,
        });
        setRefreshNonce((n) => n + 1); // the new root must appear
        return null;
      } catch (err) {
        return fsError(err, t("fs.mountFailed"));
      }
    },
    [sessionId, g.sessions, g.applySessionPatch, t],
  );

  const onMakeRootWritable = useCallback(async (rootId: string): Promise<string | null> => {
    try {
      const r = await updateFolder(rootId, { access: "rw" });
      if (!r.ok) return r.error || t("fs.switchFailed");
      setRefreshNonce((n) => n + 1); // re-list roots so the badge flips
      return null;
    } catch (err) {
      return fsError(err, t("fs.switchFailed"));
    }
  }, [t]);

  // ---- keyboard: ⌘P quick-open, ⇧⌘F search, Esc closes the open layer -------
  // (⌘B for the tree is bound separately above — left untouched.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod || e.altKey) return;
      if (!e.shiftKey && (e.key === "p" || e.key === "P")) {
        e.preventDefault();
        setSearchOpen(false);
        setQuickOpen(true);
      } else if (e.shiftKey && (e.key === "f" || e.key === "F")) {
        e.preventDefault();
        setQuickOpen(false);
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Esc closes whichever layer is open. Scoped to when one IS open so it never
  // swallows Esc from the editor or the chat while this panel is mounted.
  useEffect(() => {
    if (!quickOpen && !searchOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setQuickOpen(false);
      setSearchOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [quickOpen, searchOpen]);

  // ---- breadcrumb -----------------------------------------------------------
  const crumbs = useMemo(() => {
    if (!activeTab) return [];
    const root = roots.find((r) => r.id === activeTab.rootId);
    const parts = activeTab.path.split("/").filter(Boolean);
    return [
      { label: root?.name ?? activeTab.rootId, path: "" },
      ...parts.map((p, i) => ({ label: p, path: parts.slice(0, i + 1).join("/") })),
    ];
  }, [activeTab, roots]);

  const jumpToDir = useCallback(
    (path: string) => {
      if (!activeTab) return;
      revealDir(activeTab.rootId, path);
      setCodeTreeOpen(true); // make the jump visible (drawer in "file" mode)
    },
    [activeTab, revealDir, setCodeTreeOpen],
  );

  // ---- tree column (shared by the "side" sidebar and the "file" drawer) ------
  const treeColumn = effectiveRootId ? (
    <>
      <RootBar
        roots={roots}
        activeRootId={effectiveRootId}
        onSelectRoot={selectRoot}
        onRefresh={onRefresh}
        refreshing={rootsLoading}
        onAddFolder={onAddFolder}
        onMakeRootWritable={onMakeRootWritable}
      />
      <div className="min-h-0 flex-1 overflow-hidden">
        <FileTree
          projectSlug={PROJECT_SLUG}
          sessionId={sessionId ?? ""}
          rootId={effectiveRootId}
          revealPath={jump && jump.rootId === effectiveRootId ? jump.path : null}
          activePath={activeTab && activeTab.rootId === effectiveRootId ? activeTab.path : null}
          onOpenFile={(path) => openFile(effectiveRootId, path)}
          refreshNonce={refreshNonce}
          /* S3 decorations, both keyed root-relative. */
          gitStatus={gitStatus}
          agentTouched={agentTouched}
          /* S4 file ops — every one is refused for an ro / missing root (the
             server would refuse anyway; disabling up front is the honest UI). */
          writable={activeRoot ? activeRoot.access === "rw" && !activeRoot.missing : false}
          onCreate={onTreeCreate}
          onRename={onTreeRename}
          onMove={onTreeMove}
          onDelete={onTreeDelete}
          onCopyPath={onCopyPath}
          onReveal={onTreeReveal}
          onOpenExternal={onTreeOpenExternal}
          className="h-full"
        />
      </div>
    </>
  ) : (
    <div className="px-2 py-2 text-[11px] text-faint">{t("panel.noWorkspace")}</div>
  );

  if (!sessionId) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-xs text-faint">
        <Info size={20} />
        <span>{t("panel.noSession")}</span>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Top bar: breadcrumb + tree/layout toggles */}
      <div className="flex shrink-0 items-center gap-1 border-b border-line px-2 py-1">
        {treeVisible ? (
          <span className="min-w-0 flex-1 truncate text-[11px] text-faint">{t("panel.workspace")}</span>
        ) : (
          <Breadcrumb crumbs={crumbs} onJump={jumpToDir} />
        )}
        {/* While browsing there is nothing to toggle: the tree IS the panel. */}
        {browsing ? null : (
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            <ToolButton title={t("panel.treeToggle")} active={g.codeTreeOpen} onClick={toggleTree}>
              <FolderTree className="h-3.5 w-3.5" />
            </ToolButton>
            {wide && (
              <ToolButton
                title={effectiveMode === "side" ? t("panel.layoutFile") : t("panel.layoutSide")}
                onClick={toggleMode}
              >
                <Columns2 className="h-3.5 w-3.5" />
              </ToolButton>
            )}
          </div>
        )}
      </div>

      {notice ? (
        <div className="flex shrink-0 items-center gap-1.5 border-b border-line bg-card px-2.5 py-1 text-[11px] text-amber">
          <AlertTriangle size={12} className="shrink-0" />
          <span className="truncate">{notice}</span>
        </div>
      ) : null}

      <div className="relative flex min-h-0 flex-1">
        {treeVisible ? (
          <div
            className={cn(
              "flex min-h-0 shrink-0 flex-col bg-panel",
              // While browsing the tree gets the whole panel: there is no editor
              // to share with, so a fixed 260px column would leave dead space.
              browsing ? "flex-1" : "border-r border-line",
            )}
            style={browsing ? undefined : { width: TREE_PX }}
          >
            {treeColumn}
          </div>
        ) : null}

        {/* Editor column — hidden while BROWSING (no tab open). With nothing to
            show it would only be an empty hint, and on the default 380px panel
            that hint plus a collapsed tree IS the whole tab. */}
        <div className={cn("min-h-0 flex-1 flex-col", browsing ? "hidden" : "flex")}>
          <EditorTabs
            tabs={tabs.map((tab) => {
              const m = tabMeta[codeTabKey(tab)];
              return m ? { ...tab, dirty: m.dirty, conflict: !!m.conflict } : tab;
            })}
            activeKey={activeKey}
            onSelect={selectTab}
            onClose={closeTab}
          />

          {/* Conflict bar (S2). A refused save is ANNOUNCED and resolved by a
              human — the panel never overwrites the disk copy on its own
              (design §6 "公告而不覆盖"). */}
          {activeMeta?.conflict ? (
            <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-line bg-base px-3 py-1.5 text-[11px]">
              <AlertTriangle size={13} className="shrink-0 text-yellow" />
              <span className="text-txt">{t("conflict.title")}</span>
              <span className="text-faint">
                {t("conflict.diskVersion", { version: activeMeta.conflict.version })}
              </span>
              <div className="ml-auto flex shrink-0 items-center gap-1">
                <BarButton
                  onClick={() => (showDiff ? setShowDiff(false) : void openDiff())}
                  icon={<Columns2 size={12} />}
                >
                  {showDiff ? t("conflict.hideDiff") : t("conflict.viewDiff")}
                </BarButton>
                <BarButton
                  onClick={() => activeKey && reloadTab(activeKey)}
                  icon={<RotateCcw size={12} />}
                  title={t("conflict.reloadTitle")}
                >
                  {t("conflict.reload")}
                </BarButton>
                <BarButton
                  onClick={() => activeKey && void forceOverwrite(activeKey)}
                  icon={<Save size={12} />}
                  title={t("conflict.forceOverwriteTitle")}
                >
                  {t("conflict.forceOverwrite")}
                </BarButton>
              </div>
            </div>
          ) : null}

          <div className="min-h-0 flex-1">
            {activeTab && activeRead?.status === "ok" && activeRead.data ? (
              showDiff && activeMeta?.conflict ? (
                <ConflictDiff
                  path={activeTab.path}
                  mine={activeMeta.conflict.mine}
                  /* openDiff only opens the diff once the disk copy is loaded,
                     so this is a real string whenever the diff is reachable. */
                  theirs={activeMeta.conflict.diskText ?? ""}
                  className="h-full"
                />
              ) : activeRead.data.readonly_reason === "image" ? (
                /* Images are binary, so there is no text for Monaco to show.
                   The runtime flags them readonly_reason="image" and the bytes
                   stream from /api/code/raw (see ImagePreview). */
                <ImagePreview
                  path={activeTab.path}
                  url={codeRawUrl(
                    PROJECT_SLUG,
                    sessionId ?? "",
                    activeTab.rootId,
                    activeTab.path,
                    activeRead.data.version,
                  )}
                  className="h-full"
                />
              ) : (
                <MonacoEditor
                  /* `key` carries the model revision. Bumping it is the only way
                     to make Monaco adopt externally-changed text: its model is
                     rebuilt only when the URI (rootId + path) changes, so a
                     reload would otherwise refetch and keep showing the stale
                     buffer. Never bumped on a plain save. */
                  key={`${activeTab.rootId}/${activeTab.path}#${activeMeta?.rev ?? 0}`}
                  rootId={activeTab.rootId}
                  path={activeTab.path}
                  text={activeRead.data.text}
                  language={activeRead.data.language}
                  encoding={activeRead.data.encoding}
                  editable={activeRead.data.editable}
                  readonlyReason={activeRead.data.readonly_reason}
                  markSavedNonce={activeMeta?.savedNonce ?? 0}
                  onDirtyChange={(dirty) => {
                    // Monaco compares alternative version ids, so an undo back to
                    // the saved text reports clean again (design §4.5).
                    if (activeKey) patchMeta(activeKey, (m) => (m.dirty === dirty ? {} : { dirty }));
                  }}
                  onSave={(text) => {
                    if (activeKey) void saveTab(activeKey, text);
                  }}
                  onChangeText={(text) => {
                    if (activeKey) latestTextRef.current[activeKey] = text;
                  }}
                  revealLine={
                    jump && jump.rootId === activeTab.rootId && jump.path === activeTab.path
                      ? jump.line
                      : null
                  }
                  revealNonce={
                    jump && jump.rootId === activeTab.rootId && jump.path === activeTab.path
                      ? jump.nonce
                      : 0
                  }
                  className="h-full"
                />
              )
            ) : activeTab && activeRead?.status === "error" ? (
              <EmptyHint icon={<AlertTriangle size={18} />} text={activeRead.error ?? t("notice.readFailed")} />
            ) : activeTab ? (
              <EmptyHint text={t("panel.reading")} />
            ) : (
              <EmptyHint text={t("panel.emptyTitle")} hint={t("panel.emptyHint")} />
            )}
          </div>
        </div>

        {/* Overlay drawer: the tree's fallback when it is NOT docked — "file"
            mode with the tree switched on, so the editor keeps its width. While
            browsing the tree IS the panel, so there is no drawer. */}
        {!treeVisible && g.codeTreeOpen ? (
          <>
            <div
              className="absolute inset-0 z-10 bg-black/20"
              onClick={() => setCodeTreeOpen(false)}
              aria-hidden
            />
            <div className="absolute inset-y-0 left-0 z-20 flex w-[280px] flex-col border-r border-line bg-panel shadow-xl">
              {treeColumn}
            </div>
          </>
        ) : null}

        {/* Content search (⇧⌘F) fills the content area; picking a hit closes it
            so the opened file is visible underneath. */}
        {searchOpen ? (
          <div className="absolute inset-0 z-30 flex min-h-0 flex-col bg-panel">
            <SearchView
              search={search}
              rootName={activeRoot?.name}
              onOpen={(path, line) => {
                if (!effectiveRootId) return;
                openFile(effectiveRootId, path, line);
                setSearchOpen(false);
              }}
              onClose={() => setSearchOpen(false)}
            />
          </div>
        ) : null}
      </div>

      {/* ⌘P quick-open — its own fixed-position dialog. */}
      {quickOpen ? (
        <QuickOpen
          search={search}
          onOpen={(path) => {
            if (!effectiveRootId) return;
            openFile(effectiveRootId, path);
          }}
          onClose={() => setQuickOpen(false)}
        />
      ) : null}
    </div>
  );
}

function ToolButton({
  title,
  active = false,
  onClick,
  children,
}: {
  title: string;
  active?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded p-1 transition-colors",
        active ? "bg-card2 text-txt" : "text-faint hover:bg-card hover:text-txt",
      )}
    >
      {children}
    </button>
  );
}

function Breadcrumb({
  crumbs,
  onJump,
}: {
  crumbs: { label: string; path: string }[];
  onJump: (path: string) => void;
}) {
  const t = useTranslations("code.panel");
  if (!crumbs.length)
    return <span className="min-w-0 flex-1 truncate text-[11px] text-faint">{t("noFileOpen")}</span>;
  return (
    <div className="flex min-w-0 flex-1 items-center overflow-hidden text-[11px]">
      {crumbs.map((c, i) => (
        <span key={`${c.path}-${i}`} className="flex min-w-0 items-center">
          {i > 0 && <ChevronRight className="mx-0.5 h-3 w-3 shrink-0 text-faint" />}
          <button
            type="button"
            onClick={() => onJump(c.path)}
            title={c.path || t("breadcrumbRoot")}
            className={cn(
              "max-w-[160px] truncate rounded px-1 py-0.5 hover:bg-card2 hover:text-txt",
              i === crumbs.length - 1 ? "text-txt" : "text-muted",
            )}
          >
            {c.label}
          </button>
        </span>
      ))}
    </div>
  );
}

/** Small text button, used by the conflict bar. */
function BarButton({
  onClick,
  icon,
  title,
  children,
}: {
  onClick: () => void;
  icon?: ReactNode;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-card2 hover:text-txt"
    >
      {icon}
      {children}
    </button>
  );
}

function EmptyHint({
  icon,
  text,
  hint,
}: {
  icon?: ReactNode;
  text: string;
  hint?: string;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-xs text-faint">
      {icon}
      <span>{text}</span>
      {hint ? <span className="text-[11px] text-faint/80">{hint}</span> : null}
    </div>
  );
}