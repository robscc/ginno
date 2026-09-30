"use client";

/**
 * Lazy workspace file tree (docs/code-panel-design.md §3.4).
 *
 * One directory level is fetched per expand (`listCodeDir`), so huge repos stay
 * cheap. Heavy directories (`node_modules`, `.git`, …) come back from the server
 * flagged `hidden: true`; instead of hiding them (lying about the filesystem) or
 * listing them (unusable tree) we render a single clickable placeholder row —
 * design §3.4's central tradeoff.
 *
 * Keyboard: ↑↓ move, →← expand/collapse, Enter/Space open; the tree is a
 * `role="tree"` with a visible roving focus.
 *
 * S3 (design §3.4) adds two purely decorative, optional layers, both keyed by
 * FULL root-relative path and both inert when their prop is absent: a row-start
 * git status letter (`gitStatus`) and an accent gutter bar + dot for files the
 * agent has written/edited (`agentTouched`). They use different encodings on
 * purpose and never replace one another.
 *
 * S4 (brief §3.4) adds file operations, all driven by panel-injected callbacks
 * (this component performs no I/O of its own beyond the existing listings):
 *   - right-click menu (ContextMenu) + F2 / Delete keyboard routes;
 *   - create / rename via an INLINE input row (never `window.prompt`);
 *   - delete with an explicit confirmation that reports the directory's entry
 *     count (brief §1-6);
 *   - same-root drag & drop with POINTER EVENTS. HTML5 DnD is unusable here:
 *     `dragDropEnabled: true` in tauri.conf.json makes Tauri swallow the native
 *     drag events, and turning it off would break the app's existing file-drop
 *     (brief §1-7). The pointer pattern follows the live `PanelResizer`.
 * Cross-root moves are out of scope by design — a tree is one root, so a drop
 * target can only ever be inside it.
 */

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { ChevronDown, ChevronRight, FileText, Folder, FolderLock } from "lucide-react";
import { useGinno } from "@/lib/store";
import { CodeApiError, listCodeDir } from "@/lib/runtime";
import { cn } from "@/lib/utils";
import type { CodeEntry } from "@/lib/codeTypes";
import {
  agentMarkerOf,
  ancestorDirs,
  baseName,
  canDropInto,
  codeErrorMessage,
  flattenTree,
  fmtBytes,
  gitBadgeOf,
  initialTreeState,
  joinPath,
  moveRow,
  parentPath,
  rowId,
  ROW_LIMIT,
  treeReducer,
  validateEntryName,
  type TreeItem,
  type TreeRow,
} from "./treeUtils";
import { ContextMenu, buildFileMenuItems, type CodeContextAction } from "./ContextMenu";

/** Mirrors `code.py`'s `HIDDEN_COUNT_CAP`: a heavy dir's count arrives capped,
 *  and the cap value itself means "at least this many" (design §3.4's
 *  `已隐藏 12,483 项` vs the overflow form `已隐藏 9,999+ 项`). */
const HIDDEN_COUNT_CAP = 9999;

/** Pointer travel (px) before a press turns into a drag — below it the gesture
 *  stays a plain click, so selecting a row is never mistaken for a move. */
const DRAG_THRESHOLD = 5;

/** Callbacks return an error message (`null` = success): the tree shows the
 *  server's own wording inline, which is more useful than a generic failure. */
type OpResult = Promise<string | null> | string | null;

/** The heavy-dir placeholder's right-hand label. */
function hiddenLabel(count: number | null): string {
  if (count == null) return "已隐藏";
  if (count >= HIDDEN_COUNT_CAP) return `已隐藏 ${HIDDEN_COUNT_CAP.toLocaleString()}+ 项`;
  return `已隐藏 ${count.toLocaleString()} 项`;
}

/** What a rename / create action is currently editing. */
type Editing =
  | { kind: "new"; dir: string; type: "file" | "dir" }
  | { kind: "rename"; path: string };

/** The target of an open context menu. `"root"` is the empty area below the
 *  rows (new file / new folder at the root — the root has no row of its own). */
interface MenuTarget {
  path: string;
  name: string;
  kind: "file" | "dir" | "root";
}

interface PendingDelete {
  path: string;
  name: string;
  isDir: boolean;
  /** Immediate children, when known. `null` = not (yet) known. */
  count: number | null;
  /** The listing hit the server's entry cap, so `count` is a lower bound. */
  countCapped: boolean;
  loading: boolean;
}

export interface FileTreeProps {
  projectSlug: string;
  sessionId: string;
  /** Root id ("session" or a mount id) whose tree to render. */
  rootId: string;
  /** Root-relative file path to reveal: ancestors are expanded and loaded, the
   *  row is focused and scrolled into view (used by the chat-jump path). */
  revealPath?: string | null;
  /** Currently open file, highlighted. */
  activePath?: string | null;
  /** Defaults to the store's `openInCode` (the unified entry point). */
  onOpenFile?: (path: string) => void;
  /** Bump to force a re-list of the root and drop cached levels (manual refresh). */
  refreshNonce?: number;
  /** Root-relative path → git single-letter status (from `/api/code/git`).
   *  Keyed by FULL root-relative path; the tree looks each row up with
   *  `row.path`. Missing/empty → no decoration, never an error. */
  gitStatus?: Record<string, string> | null;
  /** Root-relative path → the op the agent last used on it (`code.changed`).
   *  The panel rebases the event's absolute path to a root-relative one before
   *  passing it in, so the tree never needs to know the root's absolute path. */
  agentTouched?: Record<string, "write" | "edit"> | null;
  /** False for an `ro` mount / missing root: create, rename, delete and drag
   *  are disabled (the server would refuse them anyway). Defaults to true. */
  writable?: boolean;

  // ---- S4 file operations (all wired by the panel; none are fetched here) ----
  /** Create a file/folder directly under `dir` (`""` = root). */
  onCreate?: (dir: string, name: string, type: "file" | "dir") => OpResult;
  /** Rename one entry in place (same directory). */
  onRename?: (path: string, name: string) => OpResult;
  /** Move `path` to the root-relative destination path `to` (same root). */
  onMove?: (path: string, to: string) => OpResult;
  /** Delete (trash) one entry. */
  onDelete?: (path: string) => OpResult;
  /** Put an absolute or root-relative path on the clipboard. */
  onCopyPath?: (path: string, absolute: boolean) => void;
  /** Reveal in Finder (Tauri `code_reveal`). */
  onReveal?: (path: string) => void;
  /** Open with the OS default app (Tauri `code_open_external`). */
  onOpenExternal?: (path: string) => void;

  className?: string;
}

export function FileTree({
  projectSlug,
  sessionId,
  rootId,
  revealPath,
  activePath,
  onOpenFile,
  refreshNonce = 0,
  gitStatus,
  agentTouched,
  writable = true,
  onCreate,
  onRename,
  onMove,
  onDelete,
  onCopyPath,
  onReveal,
  onOpenExternal,
  className,
}: FileTreeProps) {
  const { openInCode } = useGinno();
  const [state, dispatch] = useReducer(treeReducer, undefined, initialTreeState);
  const [focusedKey, setFocusedKey] = useState<string | null>(null);

  // ---- S4 local UI state ----------------------------------------------------
  const [editing, setEditing] = useState<Editing | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; target: MenuTarget } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [ghost, setGhost] = useState<{ x: number; y: number; name: string } | null>(null);
  const [dropDir, setDropDir] = useState<string | null>(null);

  // Latest state for async loops (the reveal walk needs it without re-arming).
  const stateRef = useRef(state);
  stateRef.current = state;
  // Loaded/in-flight path keys, so concurrent expands don't double-fetch.
  const loadedRef = useRef<Set<string>>(new Set());
  const loadKey = useCallback((path: string) => `${rootId}\u0000${path}`, [rootId]);

  const loadDir = useCallback(
    async (path: string): Promise<void> => {
      const key = loadKey(path);
      if (loadedRef.current.has(key)) return;
      loadedRef.current.add(key);
      dispatch({ type: "loadStart", path });
      try {
        const listing = await listCodeDir(projectSlug, sessionId, rootId, path);
        dispatch({ type: "loadOk", path, entries: listing.entries, truncated: listing.truncated });
      } catch (err) {
        // Allow a retry after a failure.
        loadedRef.current.delete(key);
        const code = err instanceof CodeApiError ? err.code : "unknown";
        dispatch({ type: "loadError", path, code });
      }
    },
    [projectSlug, sessionId, rootId, loadKey],
  );

  /** Re-list `path` after a file op changed its contents (drops the cache so
   *  `loadDir` is not short-circuited by `loadedRef`). */
  const refreshDir = useCallback(
    async (path: string): Promise<void> => {
      loadedRef.current.delete(loadKey(path));
      await loadDir(path);
    },
    [loadDir, loadKey],
  );

  const expand = useCallback(
    (path: string) => {
      dispatch({ type: "open", path });
      void loadDir(path);
    },
    [loadDir],
  );

  const collapse = useCallback((path: string) => dispatch({ type: "close", path }), []);

  const toggle = useCallback(
    (path: string) => {
      if (state.open[path]) {
        collapse(path);
      } else {
        // `path` here is the item's own directory key; expanding a child dir
        // loads it, expanding a dir we're inside re-opens its cached listing.
        expand(path);
      }
    },
    [state.open, expand, collapse],
  );

  const openFile = useCallback(
    (path: string) => {
      if (onOpenFile) onOpenFile(path);
      else openInCode({ rootId, path });
    },
    [onOpenFile, openInCode, rootId],
  );

  // Load the root level whenever the root (or session) changes, and on an
  // explicit refresh (which also drops the cached child listings).
  useEffect(() => {
    dispatch({ type: "reset" });
    loadedRef.current = new Set();
    setFocusedKey(null);
    setEditing(null);
    setMenu(null);
    setPendingDelete(null);
    void loadDir("");
  }, [loadDir, refreshNonce]);

  // Reveal a path: expand + load each ancestor, then focus the row.
  useEffect(() => {
    if (!revealPath) return;
    let cancelled = false;
    void (async () => {
      for (const dir of ancestorDirs(revealPath)) {
        if (cancelled) return;
        dispatch({ type: "open", path: dir });
        await loadDir(dir);
      }
      if (!cancelled) setFocusedKey(revealPath);
    })();
    return () => {
      cancelled = true;
    };
  }, [revealPath, rootId, loadDir]);

  const items = useMemo(() => flattenTree(state), [state]);
  const rows = useMemo(
    () => items.filter((i): i is Extract<TreeItem, { kind: "row" }> => i.kind === "row"),
    [items],
  );
  // CodeEntry by full path — `TreeRow` doesn't carry `hidden_count`, so the
  // placeholder row reads its entry here (same path key `flattenTree` builds).
  const entryByPath = useMemo(() => {
    const map = new Map<string, CodeEntry>();
    for (const [dir, entries] of Object.entries(state.listings)) {
      for (const e of entries) map.set(joinPath(dir, e.name), e);
    }
    return map;
  }, [state.listings]);

  // Keep the roving focus visible as rows appear (reveal may focus before the
  // row exists) or move.
  useEffect(() => {
    if (!focusedKey) return;
    document.getElementById(rowId(focusedKey))?.scrollIntoView({ block: "nearest" });
  }, [focusedKey, items.length]);

  // ---- S4: menu, inline editing, delete -------------------------------------
  const startNew = useCallback(
    (dir: string, type: "file" | "dir") => {
      setMenu(null);
      setEditError(null);
      setEditing({ kind: "new", dir, type });
      if (dir && !stateRef.current.open[dir]) expand(dir);
    },
    [expand],
  );

  const startRename = useCallback((path: string) => {
    setMenu(null);
    setEditError(null);
    setEditing({ kind: "rename", path });
  }, []);

  const commitNew = useCallback(
    async (name: string) => {
      const e = editing;
      if (!e || e.kind !== "new" || !onCreate) return;
      const invalid = validateEntryName(name);
      if (invalid) {
        setEditError(invalid);
        return;
      }
      try {
        const message = await onCreate(e.dir, name.trim(), e.type);
        if (message) {
          setEditError(message);
          return;
        }
        setEditing(null);
        setEditError(null);
        void refreshDir(e.dir);
      } catch (err) {
        setEditError(err instanceof Error ? err.message : "创建失败");
      }
    },
    [editing, onCreate, refreshDir],
  );

  const commitRename = useCallback(
    async (path: string, name: string) => {
      if (!onRename) return;
      const trimmed = name.trim();
      // Unchanged name → silently cancel rather than round-trip a no-op.
      if (trimmed === baseName(path)) {
        setEditing(null);
        setEditError(null);
        return;
      }
      const invalid = validateEntryName(trimmed);
      if (invalid) {
        setEditError(invalid);
        return;
      }
      try {
        const message = await onRename(path, trimmed);
        if (message) {
          setEditError(message);
          return;
        }
        setEditing(null);
        setEditError(null);
        void refreshDir(parentPath(path));
      } catch (err) {
        setEditError(err instanceof Error ? err.message : "重命名失败");
      }
    },
    [onRename, refreshDir],
  );

  const openDelete = useCallback(
    (path: string, name: string, isDir: boolean) => {
      setMenu(null);
      if (!isDir) {
        setPendingDelete({ path, name, isDir, count: null, countCapped: false, loading: false });
        return;
      }
      const loaded = stateRef.current.listings[path];
      if (loaded) {
        // The count is immediate and exact (up to the server's entry cap).
        setPendingDelete({
          path,
          name,
          isDir,
          count: loaded.length,
          countCapped: !!stateRef.current.truncated[path],
          loading: false,
        });
        return;
      }
      // Not loaded yet (e.g. a collapsed directory): ask once, so the
      // confirmation can still report how much is about to be trashed.
      setPendingDelete({ path, name, isDir, count: null, countCapped: false, loading: true });
      void (async () => {
        try {
          const listing = await listCodeDir(projectSlug, sessionId, rootId, path);
          setPendingDelete((p) =>
            p && p.path === path
              ? { ...p, count: listing.entries.length, countCapped: listing.truncated, loading: false }
              : p,
          );
        } catch {
          setPendingDelete((p) => (p && p.path === path ? { ...p, loading: false } : p));
        }
      })();
    },
    [projectSlug, sessionId, rootId],
  );

  const confirmDelete = useCallback(async () => {
    const t = pendingDelete;
    if (!t || !onDelete || deleteBusy) return;
    setDeleteBusy(true);
    try {
      const message = await onDelete(t.path);
      setPendingDelete(null);
      if (message) {
        setOpError(message);
        return;
      }
      void refreshDir(parentPath(t.path));
    } catch (err) {
      setPendingDelete(null);
      setOpError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setDeleteBusy(false);
    }
  }, [pendingDelete, onDelete, deleteBusy, refreshDir]);

  const runAction = useCallback(
    (id: string) => {
      const target = menu?.target;
      setMenu(null);
      if (!target) return;
      switch (id as CodeContextAction) {
        case "new-file":
          startNew(target.path, "file");
          break;
        case "new-folder":
          startNew(target.path, "dir");
          break;
        case "rename":
          startRename(target.path);
          break;
        case "delete":
          openDelete(target.path, target.name, target.kind === "dir");
          break;
        case "copy-rel":
          onCopyPath?.(target.path, false);
          break;
        case "copy-abs":
          onCopyPath?.(target.path, true);
          break;
        case "reveal":
          onReveal?.(target.path);
          break;
        case "open-external":
          onOpenExternal?.(target.path);
          break;
        default:
          break;
      }
    },
    [menu, startNew, startRename, openDelete, onCopyPath, onReveal, onOpenExternal],
  );

  const menuItems = useMemo(() => {
    if (!menu) return [];
    return buildFileMenuItems({
      target: menu.target.kind,
      writable,
      canCreate: !!onCreate,
      canRename: !!onRename,
      canDelete: !!onDelete,
      canCopyPath: !!onCopyPath,
      canReveal: !!onReveal,
      canOpenExternal: !!onOpenExternal,
    });
  }, [menu, writable, onCreate, onRename, onDelete, onCopyPath, onReveal, onOpenExternal]);

  const openRowMenu = useCallback((row: TreeRow, x: number, y: number) => {
    setEditing(null);
    setMenu({
      x,
      y,
      target: { path: row.path, name: row.name, kind: row.expandable ? "dir" : "file" },
    });
  }, []);

  // ---- S4: same-root drag & drop (pointer events, never HTML5 DnD) ----------
  const treeRef = useRef<HTMLDivElement>(null);
  const pressRef = useRef<{ path: string; name: string; x: number; y: number } | null>(null);
  const dragRef = useRef<{ path: string; name: string } | null>(null);
  // Mirrors `dropDir` for the pointerup listener (state would be stale there).
  const dropDirRef = useRef<string | null>(null);
  // Set after a real drag so the click that follows the pointerup does not also
  // activate the row we just dropped.
  const suppressClickRef = useRef(false);
  const onMoveRef = useRef(onMove);
  onMoveRef.current = onMove;
  // Body-level cursor lock, saved at drag start and restored at drop.
  const prevCursorRef = useRef("");

  const doMove = useCallback(
    async (from: string, toDir: string) => {
      if (!onMoveRef.current) return;
      // Dropping an entry back where it already lives is not a move; skipping
      // the round trip avoids a pointless server error on a no-op.
      if (parentPath(from) === toDir) return;
      const to = toDir ? joinPath(toDir, baseName(from)) : baseName(from);
      try {
        const message = await onMoveRef.current(from, to);
        if (message) {
          setOpError(message);
          return;
        }
        void refreshDir(parentPath(from));
        void refreshDir(toDir);
      } catch (err) {
        setOpError(err instanceof Error ? err.message : "移动失败");
      }
    },
    [refreshDir],
  );
  // The drag listeners must survive re-renders (and never be torn down while a
  // drag is live), so they read `doMove` through a ref and register once.
  const doMoveRef = useRef(doMove);
  doMoveRef.current = doMove;

  useEffect(() => {
    const endDrag = () => {
      const dragging = dragRef.current;
      const target = dropDirRef.current;
      dragRef.current = null;
      pressRef.current = null;
      dropDirRef.current = null;
      setGhost(null);
      setDropDir(null);
      if (!dragging) return;
      document.body.classList.remove("select-none");
      document.body.style.cursor = prevCursorRef.current;
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 250);
      if (target != null && canDropInto(dragging.path, target)) void doMoveRef.current(dragging.path, target);
    };

    const onPointerMove = (e: PointerEvent) => {
      const pressed = pressRef.current;
      if (!pressed) return;
      if (!dragRef.current) {
        if (Math.hypot(e.clientX - pressed.x, e.clientY - pressed.y) < DRAG_THRESHOLD) return;
        dragRef.current = { path: pressed.path, name: pressed.name };
        document.body.classList.add("select-none");
        prevCursorRef.current = document.body.style.cursor;
        document.body.style.cursor = "grabbing";
      }
      const drag = dragRef.current;
      setGhost({ x: e.clientX, y: e.clientY, name: drag.name });

      // Resolve the drop target from the element under the pointer. A directory
      // row is a valid target; the empty area below the rows means the root; a
      // FILE row means "not here" (null), never the root.
      const el = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null;
      const container = treeRef.current;
      let target: string | null = null;
      if (el && container && container.contains(el)) {
        const rowEl = el.closest("[data-row-path]");
        if (!rowEl) target = "";
        else target = el.closest("[data-row-drop]")?.getAttribute("data-row-path") ?? null;
      }
      const valid = target != null && canDropInto(drag.path, target) ? target : null;
      dropDirRef.current = valid;
      setDropDir(valid);
    };

    const onPointerCancel = () => {
      const wasDragging = !!dragRef.current;
      dragRef.current = null;
      pressRef.current = null;
      dropDirRef.current = null;
      setGhost(null);
      setDropDir(null);
      if (wasDragging) {
        document.body.classList.remove("select-none");
        document.body.style.cursor = prevCursorRef.current;
      }
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", onPointerCancel);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", onPointerCancel);
      // If the tree unmounts mid-drag, still release the body-level lock.
      document.body.classList.remove("select-none");
      document.body.style.cursor = "";
    };
  }, []);

  const beginPress = (e: React.PointerEvent<HTMLDivElement>, row: TreeRow) => {
    // No move callback → no drag affordance at all (a drag that silently does
    // nothing is worse than no drag).
    if (e.button !== 0 || !writable || !onMove || editing || pendingDelete) return;
    pressRef.current = { path: row.path, name: row.name, x: e.clientX, y: e.clientY };
  };

  // ---- keyboard --------------------------------------------------------------
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const idx = focusedKey ? rows.findIndex((r) => r.row.path === focusedKey) : -1;
    const cur: TreeRow | null = idx >= 0 ? rows[idx].row : null;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setFocusedKey(moveRow(items, focusedKey, 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setFocusedKey(moveRow(items, focusedKey, -1));
        break;
      case "ArrowRight": {
        e.preventDefault();
        if (!cur) {
          setFocusedKey(moveRow(items, focusedKey, 1));
        } else if (cur.expandable && !state.open[cur.path]) {
          expand(cur.path);
        } else if (cur.expandable) {
          setFocusedKey(rows[idx + 1]?.row.path ?? cur.path);
        }
        break;
      }
      case "ArrowLeft": {
        e.preventDefault();
        if (!cur) return;
        if (cur.expandable && state.open[cur.path]) {
          collapse(cur.path);
        } else {
          const parent = parentPath(cur.path);
          setFocusedKey(rows.find((r) => r.row.path === parent)?.row.path ?? focusedKey);
        }
        break;
      }
      case "Enter":
      case " ": {
        e.preventDefault();
        if (!cur || editing) return;
        if (cur.expandable) toggle(cur.path);
        else openFile(cur.path);
        break;
      }
      // Keyboard routes for the two operations that must never be mouse-only
      // (brief §3.4 硬约束 2). Delete is the Mac's forward-delete; ⌫ is accepted
      // too because most Mac keyboards have no dedicated Delete key.
      case "F2":
        e.preventDefault();
        if (cur && writable && onRename && !editing) startRename(cur.path);
        break;
      case "Delete":
      case "Backspace":
        e.preventDefault();
        if (cur && writable && onDelete && !editing) openDelete(cur.path, cur.name, cur.expandable);
        break;
      default:
        break;
    }
  };

  const activateRow = (row: TreeRow) => {
    setFocusedKey(row.path);
    if (row.expandable) toggle(row.path);
    else openFile(row.path);
  };

  const retry = (path: string) => {
    loadedRef.current.delete(loadKey(path));
    void loadDir(path);
  };

  const indent = (depth: number) => ({ paddingLeft: depth * 14 + 6 });

  const newAtRoot = editing?.kind === "new" && editing.dir === "" ? editing : null;

  return (
    <>
      <div
        ref={treeRef}
        role="tree"
        aria-label="文件树"
        tabIndex={0}
        aria-activedescendant={focusedKey ? rowId(focusedKey) : undefined}
        onKeyDown={onKeyDown}
        onContextMenu={(e) => {
          e.preventDefault();
          if (editing) return;
          setMenu({
            x: e.clientX,
            y: e.clientY,
            target: { path: "", name: "根目录", kind: "root" },
          });
        }}
        /* Belt and braces against the native drag layer (a text selection drag
           would otherwise race our pointer drag, and Tauri's dragDropEnabled
           makes native events behave unpredictably). */
        onDragStart={(e) => e.preventDefault()}
        className={cn(
          "relative h-full overflow-auto py-1 text-xs outline-none",
          className,
        )}
      >
        {opError ? (
          <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-card px-2 py-1 text-[11px] text-red">
            <span className="min-w-0 flex-1 truncate">{opError}</span>
            <button
              type="button"
              onClick={() => setOpError(null)}
              className="shrink-0 text-faint hover:text-txt"
            >
              关闭
            </button>
          </div>
        ) : null}

        {newAtRoot ? (
          <InlineNameInput
            depth={0}
            initial=""
            placeholder={newAtRoot.type === "file" ? "新文件名" : "新文件夹名"}
            error={editError}
            onCommit={(v) => void commitNew(v)}
            onCancel={() => {
              setEditing(null);
              setEditError(null);
            }}
          />
        ) : null}

        {items.map((item) => {
          if (item.kind === "row") {
            const { row } = item;
            const isOpen = row.expandable && !!state.open[row.path];
            const isActive = activePath === row.path;
            const isFocused = focusedKey === row.path;
            // Decorations are looked up by the row's FULL root-relative path —
            // `row.path` is exactly what `flattenTree` built via `joinPath`, and
            // the key the git endpoint returns (design §3.4, S3).
            const badge = gitBadgeOf(gitStatus?.[row.path]);
            const agent = agentMarkerOf(agentTouched?.[row.path]);
            const isDropTarget = dropDir === row.path;
            const newHere = editing?.kind === "new" && editing.dir === row.path ? editing : null;
            return (
              <Fragment key={item.id}>
                <div
                  id={item.id}
                  role="treeitem"
                  aria-level={row.depth + 1}
                  aria-expanded={row.expandable ? isOpen : undefined}
                  aria-selected={isActive}
                  data-row-path={row.path}
                  data-row-drop={row.expandable ? "1" : undefined}
                  onPointerDown={(e) => beginPress(e, row)}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openRowMenu(row, e.clientX, e.clientY);
                  }}
                  onClick={() => {
                    // A pointerup that ended a drag is followed by a click; it
                    // must not also toggle/open the row it just moved.
                    if (suppressClickRef.current) {
                      suppressClickRef.current = false;
                      return;
                    }
                    activateRow(row);
                  }}
                  style={indent(row.depth)}
                  className={cn(
                    "relative flex cursor-pointer items-center gap-1.5 rounded-md py-1 pr-2 text-muted hover:bg-line2/40",
                    isActive && "bg-line2/60 text-txt",
                    isFocused && "ring-1 ring-inset ring-indigo/70",
                    isDropTarget && "bg-indigo/15 ring-1 ring-inset ring-indigo/70",
                  )}
                >
                  {/* Agent change = GUTTER encoding: an accent bar hugging the row's
                      left edge, absolutely positioned so neither the row's indent
                      nor the chevron column can move. Deliberately not a letter, so
                      it can never be mistaken for a git status (brief §1-4). */}
                  {agent && (
                    <span
                      title={agent.title}
                      className="absolute inset-y-0.5 left-0 w-[3px] rounded-full bg-indigo"
                    />
                  )}
                  {/* Git = LETTER encoding, at the start of the row's content. A
                      fixed 10px slot keeps `M`/`!` etc. from jittering. */}
                  {badge && (
                    <span
                      title={badge.title}
                      className={cn(
                        "w-2.5 shrink-0 text-center font-mono text-[10px] font-semibold leading-none",
                        badge.className,
                      )}
                    >
                      {badge.letter}
                    </span>
                  )}
                  {/* The dot completes the agent marker. Both decorations render
                      independently: a row can be both agent-touched and modified. */}
                  {agent && (
                    <span title={agent.title} className="h-1.5 w-1.5 shrink-0 rounded-full bg-indigo" />
                  )}
                  {row.expandable ? (
                    isOpen ? (
                      <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5 shrink-0" />
                    )
                  ) : (
                    <span className="h-3.5 w-3.5 shrink-0" />
                  )}
                  {row.hidden ? (
                    <FolderLock className="h-3.5 w-3.5 shrink-0 text-faint" />
                  ) : row.type === "dir" ? (
                    <Folder
                      /* Left exactly as S1 wrote it (`text-amber` compiles to nothing
                         — see tailwind.config.ts): this rewrite must not change any
                         existing rendering. Its dead colour is a pre-existing issue. */
                      className="h-3.5 w-3.5 shrink-0 text-amber"
                    />
                  ) : (
                    <FileText className="h-3.5 w-3.5 shrink-0 text-faint" />
                  )}
                  {/* Rename replaces the name in place — inline editing, not a
                      modal (brief §3.4). */}
                  {editing?.kind === "rename" && editing.path === row.path ? (
                    <InlineNameInput
                      bare
                      initial={row.name}
                      placeholder="新名称"
                      error={editError}
                      onCommit={(v) => void commitRename(row.path, v)}
                      onCancel={() => {
                        setEditing(null);
                        setEditError(null);
                      }}
                    />
                  ) : (
                    <>
                      <span className="min-w-0 flex-1 truncate" title={row.name}>
                        {row.name}
                      </span>
                      {row.hidden ? (
                        <span className="shrink-0 text-[10px] text-faint">
                          {hiddenLabel(entryByPath.get(row.path)?.hidden_count ?? null)}
                        </span>
                      ) : (
                        row.type === "file" &&
                        row.size != null && (
                          <span className="shrink-0 text-[10px] text-faint">{fmtBytes(row.size)}</span>
                        )
                      )}
                    </>
                  )}
                </div>
                {newHere ? (
                  <InlineNameInput
                    depth={row.depth + 1}
                    initial=""
                    placeholder={newHere.type === "file" ? "新文件名" : "新文件夹名"}
                    error={editError}
                    onCommit={(v) => void commitNew(v)}
                    onCancel={() => {
                      setEditing(null);
                      setEditError(null);
                    }}
                  />
                ) : null}
              </Fragment>
            );
          }

          if (item.kind === "loading") {
            return (
              <div key={item.id} style={indent(item.depth + 1)} className="flex items-center gap-1.5 py-1">
                <span className="h-3 w-3 shrink-0 animate-pulse rounded bg-line2" />
                <span className="h-3 w-32 animate-pulse rounded bg-line2/70" />
              </div>
            );
          }

          if (item.kind === "error") {
            return (
              <div
                key={item.id}
                style={{ paddingLeft: (item.depth + 1) * 14 + 6 }}
                className="flex items-center gap-2 py-1 pr-2 text-[11px] text-red/90"
              >
                <span className="min-w-0 flex-1 truncate">{codeErrorMessage(item.code)}</span>
                <button
                  type="button"
                  onClick={() => retry(item.path)}
                  className="shrink-0 text-faint underline hover:text-txt"
                >
                  重试
                </button>
              </div>
            );
          }

          if (item.kind === "empty") {
            return (
              <div
                key={item.id}
                style={{ paddingLeft: (item.depth + 1) * 14 + 6 }}
                className="py-1 pr-2 text-[11px] text-faint"
              >
                （空）
              </div>
            );
          }

          if (item.kind === "more") {
            return (
              <div key={item.id} style={{ paddingLeft: (item.depth + 1) * 14 + 6 }} className="py-1 pr-2">
                <button
                  type="button"
                  onClick={() => dispatch({ type: "more", path: item.path })}
                  className="text-[11px] text-faint underline hover:text-txt"
                >
                  显示更多（还有 {Math.min(item.remaining, ROW_LIMIT)} 项）
                </button>
              </div>
            );
          }

          // truncated
          return (
            <div
              key={item.id}
              style={{ paddingLeft: (item.depth + 1) * 14 + 6 }}
              className="py-1 pr-2 text-[11px] text-faint"
            >
              {codeErrorMessage("truncated")}
            </div>
          );
        })}

        {/* Drag ghost: follows the pointer, never intercepts it. */}
        {ghost ? (
          <div
            style={{ left: ghost.x + 12, top: ghost.y + 8 }}
            className="pointer-events-none fixed z-50 rounded-md border border-indigo/60 bg-card px-2 py-0.5 text-[11px] text-txt shadow-lg"
          >
            {ghost.name}
          </div>
        ) : null}
      </div>

      {menu && menuItems.length ? (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          title={menu.target.name}
          items={menuItems}
          onSelect={runAction}
          onClose={() => setMenu(null)}
        />
      ) : null}

      {pendingDelete ? (
        <DeleteConfirm
          target={pendingDelete}
          busy={deleteBusy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void confirmDelete()}
        />
      ) : null}
    </>
  );
}

/** Inline single-line name editor for create / rename. Enter commits, Esc (or
 *  blur) cancels. Keys never bubble to the tree's own navigation handlers. */
function InlineNameInput({
  initial,
  placeholder,
  error,
  depth,
  bare = false,
  onCommit,
  onCancel,
}: {
  initial: string;
  placeholder: string;
  error: string | null;
  depth?: number;
  /** Render as a row-content editor (rename) instead of a standalone row. */
  bare?: boolean;
  onCommit: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) {
      el.focus();
      el.select();
    }
  }, []);

  const input = (
    <input
      ref={ref}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
        // The tree listens on the container; without this, Enter would also
        // toggle the row and Delete would open a second confirm dialog.
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          onCommit(value);
        } else if (e.key === "Escape") {
          e.preventDefault();
          onCancel();
        }
      }}
      onBlur={onCancel}
      placeholder={placeholder}
      aria-label={placeholder}
      className={cn(
        "min-w-0 flex-1 rounded border border-indigo/60 bg-card px-1.5 py-0.5 text-xs text-txt outline-none placeholder:text-faint",
        error && "border-red",
      )}
    />
  );

  if (bare) {
    return (
      <span className="flex min-w-0 flex-1 items-center gap-1.5">
        {input}
        {error ? <span className="shrink-0 text-[10px] text-red">{error}</span> : null}
      </span>
    );
  }

  return (
    <div
      style={depth != null ? { paddingLeft: depth * 14 + 6 } : undefined}
      className="flex items-center gap-1.5 py-0.5 pr-2"
    >
      <span className="h-3.5 w-3.5 shrink-0" />
      {input}
      {error ? <span className="shrink-0 text-[10px] text-red">{error}</span> : null}
    </div>
  );
}

/** Delete confirmation. Reports the directory's entry count when it is known
 *  (brief §1-6: "删除含内容的目录要报出条目数"); the wording stays honest when
 *  the count could not be read. Pointer and keyboard both work — Enter confirms,
 *  Esc cancels. */
function DeleteConfirm({
  target,
  busy,
  onCancel,
  onConfirm,
}: {
  target: PendingDelete;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  // Focus the dialog once so a keyboard user can answer it (Enter/Esc).
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  const detail = target.isDir
    ? target.loading
      ? "正在统计条目数…"
      : target.count != null
        ? `该文件夹下有 ${target.count.toLocaleString()}${target.countCapped ? "+" : ""} 项，将一并移入废纸篓。`
        : "该文件夹的内容将一并移入废纸篓。"
    : "此文件将移入废纸篓。";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onCancel}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onCancel();
        } else if (e.key === "Enter") {
          e.preventDefault();
          onConfirm();
        }
      }}
    >
      <div
        role="alertdialog"
        aria-label="确认删除"
        tabIndex={-1}
        ref={boxRef}
        className="w-[380px] max-w-[90vw] rounded-xl border border-line bg-card p-3 text-xs shadow-2xl outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 font-medium text-txt">删除「{target.name}」？</div>
        <div className="mb-1 text-muted">{detail}</div>
        <div className="mb-3 text-[11px] text-faint">可从访达的废纸篓恢复。</div>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md px-2.5 py-1 text-[11px] text-muted transition-colors hover:bg-card2 hover:text-txt"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="rounded-md bg-red px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy ? "删除中…" : "移到废纸篓"}
          </button>
        </div>
      </div>
    </div>
  );
}