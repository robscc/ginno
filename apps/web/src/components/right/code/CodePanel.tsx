"use client";

/**
 * CodePanel — composition root of the code tab (hooks split 批次 3-3).
 *
 * Assembles RootBar / FileTree / EditorTabs / MonacoEditor and wires them to
 * the panel/* domains (docs/code-panel-design.md §3.3/§3.4/§4.5):
 *   useCodeRoots  S1 roots + path mapping + refresh nonce
 *   useCodeTabs   S1/S2 tab state machine (LRU, per-tab meta, reads, jumps)
 *   useCodeEditing S2 save / conflict bar / reload / preview-mode
 *   useCodeDiff   S3 agent-change follow + inline diff scheduler
 *   useGitStatus  S3 git letters + agent marks
 *   useCodeTree   S4 file ops + tree/layout toggles (⌘B, file↔side)
 * Subcomponents: CodePanelTopBar / CodeEditorColumn(+Content) / codePanelBits.
 *
 * Layouts inside the one right-panel tab: "file" (default — tree as a ⌘B
 * overlay drawer, editor full width) and "side" (tree 260px + editor), chosen
 * automatically at ≥600px or by hand; a user choice wins over resizing, and
 * below 600px the panel is forced back to "file".
 *
 * Stage notes (docs/code-panel-design.md §4.7): ⌘S saves against a
 * `base_version` — a refusal becomes the conflict bar (重新加载 / 看差异 /
 * 强制覆盖), NEVER an automatic overwrite; `code.changed` drives a clean tab to
 * follow the file and a dirty one to the same bar; delete runs the server's
 * fence FIRST (`check_only`) before the OS trash — the Rust command cannot see
 * mount tiers, so the server must be the gate.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Info, Loader2, Server } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import { isRemoteSession, remotePeerDisplayName } from "@/components/chat/remoteGuard";
import { listCodeRoots, searchCode } from "@/lib/runtime";
import type { CodeRoot } from "@/lib/codeTypes";
import { folderToMountFor, splitRootPathIn } from "@/lib/codePaths";
import { cn } from "@/lib/utils";
import { RootBar } from "./RootBar";
import { FileTree } from "./FileTree";
import { codeTabKey } from "./EditorTabs";
import { isRenderableHtml } from "./language";
import { ChangesPanel } from "./ChangesPanel";
import { QuickOpen } from "./QuickOpen";
import { SearchView, type CodeSearchFn } from "./SearchView";
import { pickDefaultRoot, useCodeRoots } from "./panel/useCodeRoots";
import { useGitStatus } from "./panel/useGitStatus";
import { useCodeTabs } from "./panel/useCodeTabs";
import { useCodeEditing } from "./panel/useCodeEditing";
import { useCodeDiff } from "./panel/useCodeDiff";
import { useCodeTree } from "./panel/useCodeTree";
import { CodePanelTopBar } from "./panel/CodePanelTopBar";
import { CodeEditorColumn } from "./panel/CodeEditorColumn";
import { WIDE_PX, TREE_PX, PROJECT_SLUG, escapeRegExp } from "./panel/panelShared";

/** Design §3.2 guidance for a chat jump whose path is outside every root. */
interface OutsideRootGuidance {
  path: string;
  line: number | null;
  /** Directory the one-click action offers to mount; null = too broad to mount. */
  dir: string | null;
  busy: boolean;
  error: string | null;
}

export function CodePanel() {
  const g = useGinno();
  const t = useTranslations("code");
  // notice.err.<code> 是运行时动态 key(Rust 侧错误码),宽化后经 has() 门控,
  // 未命中走 err.unknown —— 与事件契约的 evText 同一防御姿态。
  const tr = t as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  // codeCmdNotice（动态错误码 i18n 门控）→ panel/useCodeTree。
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
    // 单独解构：作为 useCallback 依赖时 member 表达式会触发 exhaustive-deps
    // 的「missing dependency: 'g'」（整个 store 对象不宜入依赖）。
    applySessionPatch,
    openInCode,
  } = g;

  // ---- roots domain (panel/useCodeRoots) ------------------------------------
  // remote 守卫（displayRoots 过滤）留在本文件并被 remoteComposerGuards
  // 字符串契约钉住，故 hook 只管加载/刷新/路径映射，displayRoots 在下方 memo。
  const {
    roots,
    rootsLoading,
    refreshNonce,
    bumpRefresh,
    splitRootPath,
    absPathOf,
    onAddFolder,
    onMakeRootWritable,
  } = useCodeRoots({ sessionId, sessions: g.sessions, applySessionPatch, t: tr });
  const [notice, setNoticeState] = useState<string | null>(null);
  // §3.2 根外引导：聊天跳转带来绝对路径 = 不在任何根内。提示条给出
  // 「加为工作区」——挂载其所在目录后按常规 rooted 入口打开。
  const [outsideRoot, setOutsideRoot] = useState<OutsideRootGuidance | null>(null);
  // Any newer notice replaces the guidance; the guidance never hides a notice.
  const setNotice = useCallback((message: string | null) => {
    setNoticeState(message);
    if (message) setOutsideRoot(null);
  }, []);
  const onOutsideRoot = useCallback((req: { path: string; line: number | null } | null) => {
    setNoticeState(null);
    setOutsideRoot(
      req ? { ...req, dir: folderToMountFor(req.path), busy: false, error: null } : null,
    );
  }, []);
  // A guidance belongs to the session whose chat published it.
  useEffect(() => {
    setOutsideRoot(null);
  }, [sessionId]);
  // Panel-level overlays owned by the keyboard layer below (⌘P / ⇧⌘F).
  const [quickOpen, setQuickOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);

  // ---- per-tab editing state (S2) → panel/useCodeTabs ------------------------
  // `dirty`/`conflict`/`rev`/`savedNonce`/LRU 的语义注释随代码移入 useCodeTabs。
  // inline-diff scheduler 桥：scheduler 归 useCodeDiff，closeTab 经此取消挂起 diff。
  const inlineDiffCancelRef = useRef<(abs: string) => void>(() => {});
  const cancelInlineDiffFor = useCallback(
    (tab: { rootId: string; path: string }) => {
      const abs = absPathOf(tab);
      if (abs) inlineDiffCancelRef.current(abs);
    },
    [absPathOf],
  );

  const {
    tabs,
    activeKey,
    activeTab,
    tabMeta,
    reads,
    setReads,
    jump,
    setJump,
    setReadNonce,
    pendingClose,
    showDiff,
    setShowDiff,
    inlineDiff,
    setInlineDiff,
    tabMetaRef,
    tabsRef,
    latestTextRef,
    readsRef,
    patchMeta,
    dropRead,
    openFile,
    revealDir,
    closeTab,
    selectTab,
  } = useCodeTabs({
    sessionId,
    t: tr,
    setNotice,
    cancelInlineDiff: cancelInlineDiffFor,
    codeOpenRequest,
    clearCodeOpenRequest,
    setCodeRootId,
    onOutsideRoot,
  });
  // §3.2「加为工作区」：挂载其所在目录（与 RootBar 手动注册同一语义），
  // 成功后按常规 rooted 入口重新打开，行号随行。
  const mountOutsideRootFolder = useCallback(async () => {
    if (!outsideRoot?.dir || outsideRoot.busy || !sessionId) return;
    const { path, line, dir } = outsideRoot;
    setOutsideRoot((cur) => (cur ? { ...cur, busy: true, error: null } : cur));
    const message = await onAddFolder(dir);
    if (message) {
      setOutsideRoot((cur) => (cur ? { ...cur, busy: false, error: message } : cur));
      return;
    }
    try {
      const nextRoots = await listCodeRoots(PROJECT_SLUG, sessionId);
      const target = splitRootPathIn(nextRoots, path);
      if (!target?.rel) throw new Error("path still outside every root after mount");
      setOutsideRoot(null);
      openInCode({ rootId: target.rootId, path: target.rel, line: line ?? undefined });
    } catch {
      setOutsideRoot((cur) => (cur ? { ...cur, busy: false } : cur));
      setNotice(tr("notice.readFailed"));
    }
  }, [outsideRoot, sessionId, onAddFolder, openInCode, setNotice, tr]);
  // BROWSING = nothing open. The panel's job then is to show the tree: an empty
  // editor beside a collapsed tree is a dead pane, and since the default panel
  // (380px) is below WIDE_PX the auto-sizing below would otherwise open "file"
  // mode with the tree shut — i.e. the whole tab looking empty on entry.
  // Declared here, not with `effectiveMode`, because it reads `tabs`.
  const browsing = tabs.length === 0;
  const treeVisible = browsing || (effectiveMode === "side" && g.codeTreeOpen);
  const activeRead = activeKey ? reads[activeKey] : undefined;
  const activeMeta = activeKey ? tabMeta[activeKey] : undefined;

  // ---- absolute ↔ rooted path mapping (S3)：纯逻辑在 useCodeRoots ----------
  // （最长前缀胜。）远端会话守卫（审计 C2）留在本文件：
  // remote 会话的执行与文件落盘都在对端节点：本地「会话工作区」根恒空，
  // 列出来只会读作「文件丢了」。处理 = 说明条（主 return 顶部）解释归属 +
  // 根切换器隐藏 session 根（本地挂载的文件夹是真实本地目录，照常可浏览）。
  const activeSessionMeta = useMemo(
    () => g.sessions.find((s) => s.id === sessionId) ?? null,
    [g.sessions, sessionId],
  );
  const remoteSession = isRemoteSession(activeSessionMeta);
  const displayRoots = useMemo(
    () => (remoteSession ? roots.filter((r) => r.id !== "session") : roots),
    [remoteSession, roots],
  );
  // remote 且残留选中 session 根时回退到首个本地根——否则 FileTree 会对
  // 一个（本地）不存在的根发请求。本地会话 displayRoots === roots，零变化。
  const effectiveRootId = remoteSession
    ? codeRootId && displayRoots.some((r) => r.id === codeRootId)
      ? codeRootId
      : (displayRoots[0]?.id ?? null)
    : codeRootId ?? roots[0]?.id ?? null;
  /** The root object behind `effectiveRootId` — file ops need its absolute
   *  `path` (the Tauri commands and the clipboard take absolute paths). */
  const activeRoot = useMemo(
    () => displayRoots.find((r) => r.id === effectiveRootId) ?? null,
    [displayRoots, effectiveRootId],
  );
  /** Absolute path of a root-relative entry in the ACTIVE root. */
  const absInActiveRoot = useCallback(
    (path: string): string | null => {
      if (!activeRoot) return null;
      return path ? `${activeRoot.path}/${path}` : activeRoot.path;
    },
    [activeRoot],
  );

  // ---- 保存/冲突/重载（S2）→ panel/useCodeEditing -----------------------------
  const {
    htmlPreview,
    toggleHtmlPreview,
    acceptSave,
    saveTab,
    reloadTab,
    manualReload,
    openDiff,
    forceOverwrite,
  } = useCodeEditing({
    sessionId,
    t: tr,
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
  });

  // ---- git status for the active root (S3) → panel/useGitStatus ------------
  const { gitStatus, agentTouched } = useGitStatus({
    sessionId,
    rootId: effectiveRootId,
    refreshNonce,
    lastChange: g.codeLastChange,
    codeTouched: g.codeTouched,
    splitRootPath,
  });

  // ---- roots ----------------------------------------------------------------
  // 加载/刷新在 panel/useCodeRoots；默认根选择留在本文件（依赖 displayRoots）。
  useEffect(() => {
    if (codeRootId || !displayRoots.length) return;
    // Default to the SESSION WORKSPACE rather than `roots[0]`: the server orders
    // mounts first, so `roots[0]` is a mounted project folder when one exists —
    // but the session workspace always exists and is where the agent's own
    // output lands, so it is never an empty pane. A project folder is one click
    // away in the switcher. (Remote sessions: the workspace root is filtered
    // out of `displayRoots` above, so this falls through to the first mount.)
    const preferred = pickDefaultRoot(displayRoots);
    if (preferred) setCodeRootId(preferred.id);
  }, [codeRootId, displayRoots, setCodeRootId]);

  // ---- 以下域已拆至 panel/*：layout 自动切换与 ⌘B（useCodeTree）、打开/关闭/
  // 选择 tab（useCodeTabs）、保存/冲突/重载（useCodeEditing）。 ----
  const { exitInlineDiff, openDiffForPath } = useCodeDiff({
    sessionId,
    t: tr,
    setNotice,
    lastChange: g.codeLastChange,
    roots,
    absPathOf,
    tabsRef,
    tabMetaRef,
    latestTextRef,
    patchMeta,
    setReads,
    setInlineDiff,
    openFile,
    cancelRef: inlineDiffCancelRef,
    reloadTab,
  });

  // 「强制覆盖」与读缓存/jump-from-chat 消费分别在 useCodeEditing / useCodeTabs。

  const onRefresh = useCallback(() => {
    bumpRefresh();
    if (activeKey) {
      setReads((prev) => {
        if (!(activeKey in prev)) return prev;
        const copy = { ...prev };
        delete copy[activeKey];
        return copy;
      });
      setReadNonce((n) => n + 1);
    }
  }, [activeKey, bumpRefresh, setReads, setReadNonce]);

  const selectRoot = useCallback(
    (id: string) => {
      setCodeRootId(id);
      setJump(null);
    },
    [setCodeRootId, setJump],
  );

  // ---- S4 文件操作 → panel/useCodeTree（create/rename/move/delete/copy/reveal/external/drag）----
  const {
    toggleTree,
    toggleMode,
    onTreeCreate,
    onTreeRename,
    onTreeMove,
    onTreeDelete,
    onCopyPath,
    onDragToComposer,
    onTreeReveal,
    onTreeOpenExternal,
  } = useCodeTree({
    sessionId,
    effectiveRootId,
    activeRoot,
    absInActiveRoot,
    wide,
    effectiveMode,
    codeTreeOpen: g.codeTreeOpen,
    setCodeTreeOpen,
    setCodePanelMode,
    insertToComposer: g.insertToComposer,
    bumpRefresh,
    tr,
    setNotice,
  });

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
  // 回调本体在 panel/useCodeRoots（onAddFolder / onMakeRootWritable）。

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
        roots={displayRoots}
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
          onDragToComposer={onDragToComposer}
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
      {/* 远端会话说明条（审计 C2）：文件在对端节点落盘，本地工作区恒空。 */}
      {remoteSession && (
        <div
          data-testid="code-panel-remote-note"
          className="flex shrink-0 items-start gap-1.5 border-b border-violet/25 bg-violet/[0.06] px-2.5 py-1.5 text-[11px] leading-relaxed text-violet"
        >
          <Server className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>{t("panel.remoteNote", { name: remotePeerDisplayName(activeSessionMeta) })}</span>
        </div>
      )}
      {/* Top bar: breadcrumb + tree/layout toggles → panel/CodePanelTopBar */}
      <CodePanelTopBar
        browsing={browsing}
        treeVisible={treeVisible}
        crumbs={crumbs}
        onJumpDir={jumpToDir}
        htmlTab={!!activeTab && isRenderableHtml(activeTab.path)}
        htmlPreview={htmlPreview}
        onToggleHtmlPreview={toggleHtmlPreview}
        onReload={() => activeKey && manualReload(activeKey)}
        codeTreeOpen={g.codeTreeOpen}
        onToggleTree={toggleTree}
        wide={wide}
        sideActive={effectiveMode === "side"}
        onToggleMode={toggleMode}
      />

      {outsideRoot ? (
        <div
          data-testid="code-outside-root"
          className="flex shrink-0 items-center gap-1.5 border-b border-line bg-card px-2.5 py-1 text-[11px] text-amber"
        >
          <AlertTriangle size={12} className="shrink-0" />
          <span className="min-w-0 flex-1 truncate" title={outsideRoot.path}>
            {outsideRoot.error ?? tr("notice.outsideRoot")}
          </span>
          {/* A remote session's files live on the peer: mounting a local look-alike
              directory would only end in "absent". */}
          {outsideRoot.dir && !remoteSession && (
            <button
              type="button"
              disabled={outsideRoot.busy}
              title={tr("notice.addFolderTitle", { dir: outsideRoot.dir })}
              onClick={() => void mountOutsideRootFolder()}
              className="flex shrink-0 items-center gap-1 rounded border border-line2 px-1.5 py-0.5 text-txt transition-colors hover:bg-card2 disabled:opacity-50"
            >
              {outsideRoot.busy && <Loader2 className="h-3 w-3 animate-spin" />}
              {tr("notice.addFolder")}
            </button>
          )}
        </div>
      ) : notice ? (
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
          <CodeEditorColumn
            tabs={tabs.map((tab) => {
              const m = tabMeta[codeTabKey(tab)];
              // Agent M/A mark from the session change map (§5): the abs-path
              // key the store uses; "write" = created by the agent → "A".
              const abs = absPathOf(tab);
              const touch = abs ? g.codeTouched[abs] : undefined;
              const mark = touch ? touch.op === "write" ? ("A" as const) : ("M" as const) : undefined;
              return m ? { ...tab, dirty: m.dirty, conflict: !!m.conflict, mark } : { ...tab, mark };
            })}
            activeKey={activeKey}
            onSelect={selectTab}
            onClose={closeTab}
            conflict={activeMeta?.conflict ?? null}
            showDiff={showDiff}
            onToggleDiff={() => (showDiff ? setShowDiff(false) : void openDiff())}
            onReload={() => activeKey && manualReload(activeKey)}
            onForceOverwrite={() => activeKey && void forceOverwrite(activeKey)}
            sessionId={sessionId ?? ""}
            activeTab={activeTab}
            activeRead={activeRead}
            activeMeta={activeMeta}
            jump={jump}
            inlineDiff={inlineDiff}
            htmlPreview={htmlPreview}
            onExitInlineDiff={exitInlineDiff}
            onDirtyChange={(key, dirty) => {
              // Monaco compares alternative version ids, so an undo back to
              // the saved text reports clean again (design §4.5).
              patchMeta(key, (m) => (m.dirty === dirty ? {} : { dirty }));
              // The user started editing → leave diff mode (design §4.1).
              if (dirty) setInlineDiff((cur) => (cur?.key === key ? null : cur));
            }}
            onSave={(key, text) => void saveTab(key, text)}
            onChangeText={(key, text) => {
              latestTextRef.current[key] = text;
            }}
          />
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

      {/* §A: bottom collapsible session-changes sub-window. Self-hides its
          body when empty (a slim one-line hint remains). */}
      <ChangesPanel
        sessionId={sessionId ?? ""}
        roots={roots}
        sessionChanges={g.codeSessionChanges}
        gitStatus={gitStatus}
        gitRootId={effectiveRootId}
        onOpenDiff={openDiffForPath}
      />

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
