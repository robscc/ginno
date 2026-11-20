"use client";

/**
 * useCodeTree — the TREE / LAYOUT domain of the code panel (hooks split 批次 3-3).
 *
 * Owns:
 *   - the S4 file operations (create/rename/move/delete/copy/reveal/open-
 *     external/drag-to-composer). FileTree only renders UI; each callback
 *     resolves to a Chinese error message or `null` on success, and the tree
 *     re-lists the affected directory itself after a create/rename/move, so
 *     those paths deliberately do NOT bump `refreshNonce` (it would collapse
 *     the whole tree).
 *   - the layout toggles (tree open/close, "file"↔"side", the auto-sizing
 *     effect and ⌘B). A user choice wins: once the mode is toggled by hand,
 *     resizing no longer overrides it.
 */

import { useCallback, useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  codeDelete,
  codeMkdir,
  codeMove,
  codeRename,
} from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import type { CodeRoot } from "@/lib/codeTypes";
import { PROJECT_SLUG, codeCmdErrParts, fsError } from "./panelShared";
import type { PanelT } from "./useCodeRoots";

/** The widened i18n handle the panel already builds (`t as unknown as …`):
 *  dynamic notice.err.<code> keys resolved through has(), unknown → fallback. */
export type PanelTr = PanelT & { has(key: string): boolean };

export interface UseCodeTreeOpts {
  sessionId: string | null;
  /** Root the tree shows (panel's effectiveRootId). */
  effectiveRootId: string | null;
  activeRoot: CodeRoot | null;
  /** Absolute path of a root-relative entry in the ACTIVE root. */
  absInActiveRoot: (path: string) => string | null;
  /** Panel width ≥ WIDE_PX. */
  wide: boolean;
  effectiveMode: "file" | "side";
  codeTreeOpen: boolean;
  setCodeTreeOpen: (open: boolean) => void;
  setCodePanelMode: (m: "file" | "side") => void;
  insertToComposer: (text: string) => void;
  bumpRefresh: () => void;
  tr: PanelTr;
  setNotice: (s: string | null) => void;
}

export function useCodeTree(opts: UseCodeTreeOpts) {
  const {
    sessionId,
    effectiveRootId,
    activeRoot,
    absInActiveRoot,
    wide,
    effectiveMode,
    codeTreeOpen,
    setCodeTreeOpen,
    setCodePanelMode,
    insertToComposer,
    bumpRefresh,
    tr,
    setNotice,
  } = opts;

  // notice.err.<code> 是运行时动态 key(Rust 侧错误码),宽化后经 has() 门控,
  // 未命中走 err.unknown —— 与事件契约的 evText 同一防御姿态。
  const codeCmdNotice = useCallback(
    (p: { code: string; detail: string }): string => {
      const key = `notice.err.${p.code}`;
      return tr.has(key)
        ? tr(key, { detail: p.detail })
        : tr("notice.err.unknown", { detail: p.detail || p.code });
    },
    [tr],
  );

  // Once the user picks a layout by hand, auto-sizing must not override it.
  const modeTouchedRef = useRef(false);
  // ---- layout auto-sizing ---------------------------------------------------
  useEffect(() => {
    if (modeTouchedRef.current) return;
    const next: "file" | "side" = wide ? "side" : "file";
    setCodePanelMode(next);
    setCodeTreeOpen(wide);
  }, [wide, setCodePanelMode, setCodeTreeOpen]);

  // ---- manual controls ------------------------------------------------------
  const toggleTree = useCallback(() => {
    const opening = !codeTreeOpen;
    setCodeTreeOpen(opening);
    // On a wide panel, the manual way into "side" is opening the tree.
    if (opening && wide && effectiveMode === "file") {
      modeTouchedRef.current = true;
      setCodePanelMode("side");
    }
  }, [codeTreeOpen, wide, effectiveMode, setCodeTreeOpen, setCodePanelMode]);

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

  // ---- S4 file operations: the panel owns every request ----------------------
  // FileTree only renders UI; each callback resolves to a Chinese error message
  // (shown inline by the tree) or `null` on success. The tree re-lists the
  // affected directory itself after a create / rename / move, so those paths
  // must NOT bump `refreshNonce` — that would reset (collapse) the whole tree.

  /** Create directly under `dir` (`""` = root). `name` is one component. */
  const onTreeCreate = useCallback(
    async (dir: string, name: string, kind: "file" | "dir"): Promise<string | null> => {
      if (!sessionId || !effectiveRootId) return tr("panel.noWorkspace");
      const path = dir ? `${dir}/${name}` : name;
      try {
        await codeMkdir(PROJECT_SLUG, sessionId, effectiveRootId, path, kind);
        return null;
      } catch (err) {
        return fsError(err, tr("fs.createFailed"));
      }
    },
    [sessionId, effectiveRootId, tr],
  );

  /** Rename in place. FileTree hands over a BARE new name; the endpoint wants
   *  the full root-relative `to`, so re-attach the source's parent here. */
  const onTreeRename = useCallback(
    async (path: string, name: string): Promise<string | null> => {
      if (!sessionId || !effectiveRootId) return tr("panel.noWorkspace");
      const cut = path.lastIndexOf("/");
      const to = cut < 0 ? name : `${path.slice(0, cut)}/${name}`;
      try {
        await codeRename(PROJECT_SLUG, sessionId, effectiveRootId, path, to);
        return null;
      } catch (err) {
        return fsError(err, tr("fs.renameFailed"));
      }
    },
    [sessionId, effectiveRootId, tr],
  );

  /** Drag & drop move. `to` is already the full root-relative destination. */
  const onTreeMove = useCallback(
    async (from: string, to: string): Promise<string | null> => {
      if (!sessionId || !effectiveRootId) return tr("panel.noWorkspace");
      try {
        await codeMove(PROJECT_SLUG, sessionId, effectiveRootId, from, to);
        return null;
      } catch (err) {
        return fsError(err, tr("fs.moveFailed"));
      }
    },
    [sessionId, effectiveRootId, tr],
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
      if (!sessionId || !effectiveRootId || !activeRoot) return tr("panel.noWorkspace");
      let count: number;
      try {
        ({ count } = await codeDelete(PROJECT_SLUG, sessionId, effectiveRootId, path, {
          checkOnly: true,
        }));
      } catch (err) {
        return fsError(err, tr("fs.deleteFailed")); // fence refused — do NOT continue
      }
      const abs = absInActiveRoot(path);
      try {
        if (isDesktop()) {
          if (!abs) return tr("panel.noWorkspace");
          await invoke("code_trash", { root: activeRoot.path, path: abs });
        } else {
          await codeDelete(PROJECT_SLUG, sessionId, effectiveRootId, path, {
            confirm: true,
            confirmCount: count,
          });
        }
      } catch (err) {
        const coded = codeCmdErrParts(err);
        return coded ? codeCmdNotice(coded) : fsError(err, tr("fs.deleteFailed"));
      }
      bumpRefresh();
      return null;
    },
    [sessionId, effectiveRootId, activeRoot, absInActiveRoot, tr, codeCmdNotice, bumpRefresh],
  );

  const onCopyPath = useCallback(
    (path: string, absolute: boolean) => {
      const text = absolute ? absInActiveRoot(path) ?? path : path;
      void navigator.clipboard
        ?.writeText(text)
        .then(() => setNotice(tr("notice.copied", { path: text })))
        .catch(() => setNotice(tr("notice.copyFailed")));
    },
    [absInActiveRoot, tr, setNotice],
  );

  // 拖树行到输入框：一律插 root-relative 路径（2026-10-12 定稿，撤销此前
  // 「cwd 基准根之外用绝对路径」的折算——agent 知道各挂载根，树内相对路径
  // 就够了；根目录一级文件的裸文件名是相对路径的预期形态）。
  const onDragToComposer = useCallback((rel: string) => insertToComposer(rel), [insertToComposer]);

  // Reveal / open-external are Tauri-only; invoking them in a plain browser
  // throws, so gate on `isDesktop()` and pass ABSOLUTE paths (the shell
  // re-checks containment against the root it is given).
  const onTreeReveal = useCallback(
    (path: string) => {
      if (!isDesktop() || !activeRoot) return;
      const abs = absInActiveRoot(path);
      if (!abs) return;
      invoke("code_reveal", { root: activeRoot.path, path: abs }).catch((err) => {
        const coded = codeCmdErrParts(err);
        setNotice(
          coded
            ? codeCmdNotice(coded)
            : typeof err === "string"
              ? err
              : tr("notice.revealFailed"),
        );
      });
    },
    [activeRoot, absInActiveRoot, tr, codeCmdNotice, setNotice],
  );

  const onTreeOpenExternal = useCallback(
    (path: string) => {
      if (!isDesktop() || !activeRoot) return;
      const abs = absInActiveRoot(path);
      if (!abs) return;
      invoke("code_open_external", { root: activeRoot.path, path: abs }).catch((err) => {
        const coded = codeCmdErrParts(err);
        setNotice(
          coded
            ? codeCmdNotice(coded)
            : typeof err === "string"
              ? err
              : tr("notice.openExternalFailed"),
        );
      });
    },
    [activeRoot, absInActiveRoot, tr, codeCmdNotice, setNotice],
  );

  return {
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
  };
}
