"use client";

import { useCallback, useRef, useState } from "react";

/** An agent write/edit observed on the session socket (design §4.7 S3). */
export interface CodeTouch {
  op: "write" | "edit";
  /** The file's version right after the agent's write (``size:mtime_ns``). The
   *  code panel reuses it as the conflict baseline for a DIRTY tab, which is
   *  why the WS event carries it at all (S3 brief §0). */
  version: string;
  /** Epoch ms — newest-wins when several changes land in one turn. */
  at: number;
}

/** A pending "open this file in the code panel" request, published by the chat
 *  jump path (openInCode) and consumed by the panel (docs/code-panel-design.md
 *  §3.4). The nonce bumps on every call so re-opening the same path still
 *  re-triggers reveal/line positioning. `line` is S1-only state: the panel
 *  records it and Monaco highlights it once wired. */
export interface CodeOpenRequest {
  rootId: string;
  path: string;
  line?: number;
  nonce: number;
}

/** A pending "insert this text at the composer caret" request, published by the
 *  code panel (tree row dragged onto the composer) and consumed by ChatStream.
 *  Same nonce pattern as CodeOpenRequest: inserting the same path twice still
 *  re-triggers. Pure UI bridge — never persisted. */
export interface ComposerInsertRequest {
  text: string;
  nonce: number;
}

export function useCodePanel({
  setRightTab,
  rightPanelOpenRef,
  setRightPanelOpen,
}: {
  setRightTab: (tab: "code") => void;
  rightPanelOpenRef: { current: boolean };
  setRightPanelOpen: (open: boolean) => void;
}) {
  // The panel-open ref is stable; wrapping it locally keeps openInCode's
  // dependency array identical to the pre-split store.
  const handlesRef = useRef({ rightPanelOpenRef });
  // ---- code panel (docs/code-panel-design.md §4.5) ----
  const [codeRootId, setCodeRootIdState] = useState<string | null>(null);
  const [codePanelMode, setCodePanelModeState] = useState<"file" | "side">("file");
  const [codeTreeOpen, setCodeTreeOpenState] = useState(false);
  const [codeOpenRequest, setCodeOpenRequest] = useState<CodeOpenRequest | null>(null);
  // Agent file changes, pushed over the session socket (design §4.7 S3). The
  // cumulative map feeds the tree's marks; `codeLastChange` exists so a
  // consumer reacts once per event rather than rescanning the map.
  const [codeTouched, setCodeTouched] = useState<Record<string, CodeTouch>>({});
  const [codeLastChange, setCodeLastChange] = useState<
    (CodeTouch & { path: string; nonce: number }) | null
  >(null);
  const codeChangeNonceRef = useRef(0);
  const notifyCodeChange = useCallback(
    (change: { path: string; op: "write" | "edit"; version: string }) => {
      const touch: CodeTouch = { op: change.op, version: change.version, at: Date.now() };
      setCodeTouched((prev) => ({ ...prev, [change.path]: touch }));
      codeChangeNonceRef.current += 1;
      setCodeLastChange({ ...touch, path: change.path, nonce: codeChangeNonceRef.current });
    },
    [],
  );
  const codeOpenNonceRef = useRef(0);
  // ---- composer insert (tree → composer drag) ----
  const [composerInsert, setComposerInsert] = useState<ComposerInsertRequest | null>(null);
  const composerInsertNonceRef = useRef(0);
  const [composerDragOver, setComposerDragOver] = useState(false);

  // ---- code panel actions (docs/code-panel-design.md §3.4) ----
  const setCodeRootId = useCallback((id: string) => {
    setCodeRootIdState(id);
  }, []);
  const setCodePanelMode = useCallback((m: "file" | "side") => {
    setCodePanelModeState(m);
  }, []);
  const setCodeTreeOpen = useCallback((v: boolean) => {
    setCodeTreeOpenState(v);
  }, []);
  const clearCodeOpenRequest = useCallback(() => {
    setCodeOpenRequest(null);
  }, []);
  // 树行拖到输入框（pointer 拖拽，非 HTML5 DnD）：发布一次性插入请求，由
  // ChatStream 在光标处落地。nonce 保证同一路径连续两次拖入也重新触发。
  const insertToComposer = useCallback((text: string) => {
    composerInsertNonceRef.current += 1;
    setComposerInsert({ text, nonce: composerInsertNonceRef.current });
  }, []);
  const clearComposerInsert = useCallback(() => {
    setComposerInsert(null);
  }, []);
  // Unified jump-from-chat entry point: switch to the code tab, open the panel
  // if collapsed, and publish an open request. Deliberately does NOT touch the
  // width (design §3.4: respecting the user's width; the tree auto-collapses so
  // a narrow panel stays usable). The panel expands the tree to the file and
  // opens it; `line` rides along for Monaco's reveal.
  const openInCode = useCallback(
    (target: { rootId: string; path: string; line?: number }) => {
      setCodeRootIdState(target.rootId);
      setRightTab("code");
      if (!handlesRef.current.rightPanelOpenRef.current) setRightPanelOpen(true);
      codeOpenNonceRef.current += 1;
      setCodeOpenRequest({ ...target, nonce: codeOpenNonceRef.current });
    },
    [setRightTab, setRightPanelOpen],
  );

  return {
    codeRootId,
    codePanelMode,
    codeTreeOpen,
    codeOpenRequest,
    codeTouched,
    codeLastChange,
    notifyCodeChange,
    composerInsert,
    composerDragOver,
    setComposerDragOver,
    setCodeRootId,
    setCodePanelMode,
    setCodeTreeOpen,
    clearCodeOpenRequest,
    insertToComposer,
    clearComposerInsert,
    openInCode,
  };
}
