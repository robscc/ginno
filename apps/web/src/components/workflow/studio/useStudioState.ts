"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";

export type StudioTab = "design" | "run" | "sup" | "versions";
export type Pos = { x: number; y: number };

export interface StudioState {
  wfId: string | null;
  tab: StudioTab;
  runId: string | null;
  nodeId: string | null;
  ctxOverride: Record<string, unknown>;
  /** Cosmetic node positions — deliberately client-only, never persisted. */
  posOverrides: Record<string, Pos>;
}

type Action =
  | { type: "selectWorkflow"; id: string }
  | { type: "tab"; tab: StudioTab }
  | { type: "run"; id: string | null }
  | { type: "node"; id: string | null }
  | { type: "ctx"; ctx: Record<string, unknown> }
  | { type: "pos"; id: string; pos: Pos }
  | { type: "restore"; state: Partial<StudioState> };

const TABS: StudioTab[] = ["design", "run", "sup", "versions"];

function reducer(s: StudioState, a: Action): StudioState {
  switch (a.type) {
    case "selectWorkflow":
      if (s.wfId === a.id) return s;
      // switching workflow resets everything that pointed at the old one
      return { ...s, wfId: a.id, runId: null, nodeId: null, ctxOverride: {}, posOverrides: {} };
    case "tab":
      return s.tab === a.tab ? s : { ...s, tab: a.tab, nodeId: a.tab === "design" ? s.nodeId : null };
    case "run":
      return { ...s, runId: a.id };
    case "node":
      return { ...s, nodeId: a.id };
    case "ctx":
      return { ...s, ctxOverride: a.ctx };
    case "pos":
      return { ...s, posOverrides: { ...s.posOverrides, [a.id]: a.pos } };
    case "restore":
      return { ...s, ...a.state };
    default:
      return s;
  }
}

/** `#wf=<id>&run=<id>&node=<id>&tab=run` — hash, not searchParams: the app is a
 *  static export and `useSearchParams` would force a Suspense boundary. */
function parseHash(): Partial<StudioState> {
  if (typeof window === "undefined") return {};
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return {};
  const q = new URLSearchParams(raw);
  const out: Partial<StudioState> = {};
  const wf = q.get("wf");
  if (wf) out.wfId = wf;
  const run = q.get("run");
  if (run) out.runId = run;
  const node = q.get("node");
  if (node) out.nodeId = node;
  const tab = q.get("tab");
  if (tab && (TABS as string[]).includes(tab)) out.tab = tab as StudioTab;
  return out;
}

export function studioHash(s: Pick<StudioState, "wfId" | "tab" | "runId" | "nodeId">): string {
  const q = new URLSearchParams();
  if (s.wfId) q.set("wf", s.wfId);
  if (s.tab && s.tab !== "design") q.set("tab", s.tab);
  if (s.runId) q.set("run", s.runId);
  if (s.nodeId) q.set("node", s.nodeId);
  const enc = q.toString();
  return enc ? `#${enc}` : "";
}

export function useStudioState() {
  const [state, dispatch] = useReducer(reducer, {
    wfId: null,
    tab: "design",
    runId: null,
    nodeId: null,
    ctxOverride: {},
    posOverrides: {},
  } as StudioState);

  // Adopt the pending deep link on mount. sessionStorage is the PRIMARY
  // channel (written by the jump site, read-once): Next App Router mounts the
  // target page BEFORE committing the pushed URL, so location.hash can still
  // be empty at mount — reading it raced and let the first-recipe fallback
  // overwrite the correct target (the「每次都跳到 rewrite」bug, 2026-09-26).
  // location.hash stays as the secondary (reload/share) path.
  const booted = useRef(false);
  // Set SYNCHRONOUSLY in the boot effect, before the restore dispatch commits:
  // the shell's first-recipe fallback runs in the same commit and would
  // otherwise clobber the pending restore (second half of the same bug).
  const deepLinkRef = useRef<Partial<StudioState> | null>(null);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    let restore: Partial<StudioState> = {};
    try {
      const pending = sessionStorage.getItem("ginno:studio-deeplink");
      if (pending) {
        sessionStorage.removeItem("ginno:studio-deeplink");
        const q = new URLSearchParams(pending.replace(/^#/, ""));
        const wf = q.get("wf");
        const run = q.get("run");
        const node = q.get("node");
        const tab = q.get("tab");
        if (wf) restore.wfId = wf;
        if (run) restore.runId = run;
        if (node) restore.nodeId = node;
        if (tab && (TABS as string[]).includes(tab)) restore.tab = tab as StudioTab;
      }
    } catch {
      /* private mode — fall through to the hash */
    }
    if (!Object.keys(restore).length) restore = parseHash();
    if (Object.keys(restore).length) {
      deepLinkRef.current = restore;
      dispatch({ type: "restore", state: restore });
    }
  }, []);

  // Keep the hash in step with the selection so the view is shareable and the
  // browser back button moves within the Studio.
  useEffect(() => {
    if (typeof window === "undefined") return;
    // Don't write before a workflow is chosen: the initial state would wipe
    // the incoming hash to pathname-only, joining the mount race above.
    if (!state.wfId) return;
    const next = studioHash(state);
    if (window.location.hash === next) return;
    window.history.replaceState(null, "", next || window.location.pathname);
  }, [state.wfId, state.tab, state.runId, state.nodeId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onHash = () => {
      const restore = parseHash();
      if (Object.keys(restore).length) dispatch({ type: "restore", state: restore });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const actions = {
    /** True when a deep link was consumed on mount — the shell's
     *  first-recipe fallback must stand down for one commit. */
    hasDeepLink: useCallback(() => deepLinkRef.current !== null, []),
    selectWorkflow: useCallback((id: string) => dispatch({ type: "selectWorkflow", id }), []),
    setTab: useCallback((tab: StudioTab) => dispatch({ type: "tab", tab }), []),
    selectRun: useCallback((id: string | null) => dispatch({ type: "run", id }), []),
    selectNode: useCallback((id: string | null) => dispatch({ type: "node", id }), []),
    setCtx: useCallback((ctx: Record<string, unknown>) => dispatch({ type: "ctx", ctx }), []),
    moveNode: useCallback((id: string, pos: Pos) => dispatch({ type: "pos", id, pos }), []),
  };

  return { state, ...actions };
}