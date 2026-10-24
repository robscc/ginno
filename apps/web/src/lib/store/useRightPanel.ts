"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ALL_RIGHT_TAB_IDS, canonicalTabOrder, visibleTabOrder } from "../rightTabs";

export type RightTab = "todo" | "workflow" | "artifacts" | "memory" | "synthesis" | "code";

// Right panel width bounds (right-panel-redesign.md §3.4). The panel renders
// at `rightPanelWidth`; dragging clamps into this range, double-click resets.
export const PANEL_WIDTH_MIN = 280;
export const PANEL_WIDTH_MAX = 560;
export const PANEL_WIDTH_DEFAULT = 380;
// The code tab is allowed a much wider panel — it holds a tree + editor
// (docs/code-panel-design.md §4.5): min(50vw, this cap).
export const PANEL_WIDTH_MAX_CODE = 1100;

/** Per-tab right-panel widths, all starting at the shared default. Width is
 *  remembered per tab (design §4.5) so switching tabs restores that tab's own
 *  width instead of forcing one global number. */
export function defaultPanelWidths(): Record<RightTab, number> {
  return {
    todo: PANEL_WIDTH_DEFAULT,
    workflow: PANEL_WIDTH_DEFAULT,
    artifacts: PANEL_WIDTH_DEFAULT,
    memory: PANEL_WIDTH_DEFAULT,
    synthesis: PANEL_WIDTH_DEFAULT,
    code: PANEL_WIDTH_DEFAULT,
  };
}

/** Max right-panel width for a tab (design §4.5): the code tab may grow to
 *  half the viewport (capped at PANEL_WIDTH_MAX_CODE); every other tab keeps
 *  the historical 560px cap. Evaluated on demand so it tracks the window size
 *  while dragging. */
export function panelWidthMax(tab: RightTab): number {
  if (tab !== "code") return PANEL_WIDTH_MAX;
  const vw = typeof window !== "undefined" ? window.innerWidth : 0;
  if (!vw) return PANEL_WIDTH_MAX_CODE;
  return Math.max(PANEL_WIDTH_MIN, Math.min(0.5 * vw, PANEL_WIDTH_MAX_CODE));
}

// localStorage key for the persisted right-panel prefs.
// Current shape: {open, widths: {tab: px}}. Legacy {open, width} is still read
// (the single width migrates onto the tab active at hydration time).
const PANEL_PREFS_KEY = "ginno-right-panel";

export function useRightPanel() {
  // Right panel: tab is store-owned so chat events can auto-switch to
  // Artifacts when the active session gains one (docs §7.6). Manual clicks
  // turn autoFollow off (sticky) so the agent can't yank focus repeatedly;
  // visiting Artifacts manually or starting fresh re-enables it. Default is
  // Artifacts — first tab of the reordered bar (right-panel-redesign.md §3.1).
  const [rightTab, setRightTabState] = useState<RightTab>("artifacts");
  const [artifactsFollow, setArtifactsFollow] = useState(true);
  const artifactsFollowRef = useRef(true);
  useEffect(() => {
    artifactsFollowRef.current = artifactsFollow;
  }, [artifactsFollow]);
  const [flashArtifactIds, setFlashArtifactIds] = useState<string[]>([]);

  // ---- right panel open/width + collapsed badges (right-panel-redesign.md) ----
  // Defaults match the pre-redesign behavior (open, 380px) so upgrading users
  // see no sudden change. Persisted as one JSON blob (`ginno-right-panel`).
  const [rightPanelOpen, setRightPanelOpenState] = useState(true);
  // Width is per tab (design §4.5): the active tab's entry is what the panel
  // renders, so a switch reads the new tab's remembered width. The ref mirrors
  // the record for the persist callback (writes must not read stale state).
  const [rightPanelWidthByTab, setRightPanelWidthByTab] = useState<Record<RightTab, number>>(
    defaultPanelWidths,
  );
  const rightPanelWidth = rightPanelWidthByTab[rightTab];
  const [panelBadge, setPanelBadge] = useState<Partial<Record<RightTab, number>>>({});
  // Tab order + visibility (Settings → 通用设置). The order holds EVERY tab —
  // a hidden one keeps its slot, so unhiding restores its position instead of
  // dropping it at the end. Visibility is a separate set.
  const [rightTabOrder, setRightTabOrderState] = useState<RightTab[]>(ALL_RIGHT_TAB_IDS);
  const [rightTabsHidden, setRightTabsHiddenState] = useState<RightTab[]>([]);
  const rightTabOrderRef = useRef<RightTab[]>(ALL_RIGHT_TAB_IDS);
  const rightTabsHiddenRef = useRef<RightTab[]>([]);
  // Cheap enough to recompute per render (six ids) — no memo needed.
  const visibleRightTabs = visibleTabOrder(rightTabOrder, rightTabsHidden);
  const rightPanelOpenRef = useRef(true);
  const rightPanelWidthByTabRef = useRef<Record<RightTab, number>>(rightPanelWidthByTab);
  // Active tab mirror for callbacks that act on "the current tab" without
  // re-creating on every tab switch (persist / setWidth / legacy migration).
  const rightTabRef = useRef<RightTab>(rightTab);
  useEffect(() => {
    rightTabRef.current = rightTab;
  }, [rightTab]);

  // Artifact ids that arrived while collapsed — replayed as a pulse on reopen
  // so the highlight isn't lost to the hidden window.
  const pendingFlashRef = useRef<string[]>([]);

  const persistPanelPrefs = useCallback(() => {
    try {
      localStorage.setItem(
        PANEL_PREFS_KEY,
        JSON.stringify({
          open: rightPanelOpenRef.current,
          widths: rightPanelWidthByTabRef.current,
          order: rightTabOrderRef.current,
          hidden: rightTabsHiddenRef.current,
        }),
      );
    } catch {
      /* storage unavailable */
    }
  }, []);

  // Hydrate persisted prefs once on the client (SSR renders defaults, the
  // effect fixes them up after mount — same pattern as ginno-theme).
  useEffect(() => {
    try {
      const raw = localStorage.getItem(PANEL_PREFS_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) as {
        open?: unknown;
        width?: unknown;
        widths?: unknown;
        order?: unknown;
        hidden?: unknown;
      };
      if (typeof v.open === "boolean") {
        rightPanelOpenRef.current = v.open;
        setRightPanelOpenState(v.open);
      }
      if (v.widths && typeof v.widths === "object") {
        // Current format {open, widths:{tab:px}}.
        const saved = v.widths as Partial<Record<RightTab, number>>;
        const next = defaultPanelWidths();
        (Object.keys(next) as RightTab[]).forEach((tab) => {
          const w = saved[tab];
          if (typeof w === "number" && Number.isFinite(w)) {
            next[tab] = Math.min(panelWidthMax(tab), Math.max(PANEL_WIDTH_MIN, Math.round(w)));
          }
        });
        rightPanelWidthByTabRef.current = next;
        setRightPanelWidthByTab(next);
      } else if (typeof v.width === "number") {
        // Legacy format {open, width}: one width for every tab. Migrate it onto
        // the tab active at hydration time so the user keeps their setting;
        // the other tabs start at the default.
        const w = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, Math.round(v.width)));
        const tab = rightTabRef.current;
        const next = { ...rightPanelWidthByTabRef.current, [tab]: w };
        rightPanelWidthByTabRef.current = next;
        setRightPanelWidthByTab(next);
      }
      // Tab order/visibility. Canonicalised, so a stale preference (a tab that
      // no longer exists, or one shipped since it was saved) resolves safely.
      const order = canonicalTabOrder(v.order);
      const hidden = Array.isArray(v.hidden)
        ? v.hidden.filter((h): h is RightTab => typeof h === "string")
        : [];
      rightTabOrderRef.current = order;
      rightTabsHiddenRef.current = hidden;
      setRightTabOrderState(order);
      setRightTabsHiddenState(hidden);
      // The default tab may be hidden — land on the first visible one, or the
      // panel would open onto an empty strip.
      const vis = visibleTabOrder(order, hidden);
      if (!vis.includes(rightTabRef.current)) {
        rightTabRef.current = vis[0];
        setRightTabState(vis[0]);
      }
    } catch {
      /* corrupted prefs — keep defaults */
    }
  }, []);

  const setRightPanelOpen = useCallback(
    (open: boolean) => {
      rightPanelOpenRef.current = open;
      setRightPanelOpenState(open);
      if (open) {
        // Reopening consumes the badges and replays the missed-arrival pulse.
        setPanelBadge({});
        if (pendingFlashRef.current.length) {
          const ids = pendingFlashRef.current;
          pendingFlashRef.current = [];
          setFlashArtifactIds(ids);
          window.setTimeout(() => setFlashArtifactIds([]), 2500);
        }
      }
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  const setRightPanelWidth = useCallback(
    (w: number) => {
      const tab = rightTabRef.current;
      const cw = Math.min(panelWidthMax(tab), Math.max(PANEL_WIDTH_MIN, Math.round(w)));
      // Update the ref eagerly (persistPanelPrefs reads it synchronously).
      const next = { ...rightPanelWidthByTabRef.current, [tab]: cw };
      rightPanelWidthByTabRef.current = next;
      setRightPanelWidthByTab(next);
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  const clearPanelBadge = useCallback((tab?: RightTab) => {
    setPanelBadge((prev) => {
      if (!tab) return Object.keys(prev).length ? {} : prev;
      if (!(tab in prev)) return prev;
      const next = { ...prev };
      delete next[tab];
      return next;
    });
  }, []);

  /** Persist a new tab order. Canonicalised, so callers may pass a partial or
   *  stale list safely (the settings UI always passes a complete one). */
  const setRightTabOrder = useCallback(
    (order: RightTab[]) => {
      const next = canonicalTabOrder(order);
      rightTabOrderRef.current = next;
      setRightTabOrderState(next);
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  const setRightTabHidden = useCallback(
    (id: RightTab, hidden: boolean) => {
      const visibleNow = visibleTabOrder(rightTabOrderRef.current, rightTabsHiddenRef.current);
      // Refuse to hide the last visible tab: an empty strip leaves the panel
      // with nothing to click, so it could never be brought back.
      if (hidden && visibleNow.length <= 1 && visibleNow[0] === id) return;
      const next = hidden
        ? Array.from(new Set([...rightTabsHiddenRef.current, id]))
        : rightTabsHiddenRef.current.filter((t) => t !== id);
      rightTabsHiddenRef.current = next;
      setRightTabsHiddenState(next);
      // A hidden tab must not stay active, or the body would render a panel
      // whose tab is no longer in the strip.
      if (hidden && rightTabRef.current === id) {
        const fallback = visibleTabOrder(rightTabOrderRef.current, next)[0];
        rightTabRef.current = fallback;
        setRightTabState(fallback);
      }
      persistPanelPrefs();
    },
    [persistPanelPrefs],
  );

  /** Back to the shipped order with every tab shown. */
  const resetRightTabs = useCallback(() => {
    rightTabOrderRef.current = ALL_RIGHT_TAB_IDS;
    rightTabsHiddenRef.current = [];
    setRightTabOrderState(ALL_RIGHT_TAB_IDS);
    setRightTabsHiddenState([]);
    persistPanelPrefs();
  }, [persistPanelPrefs]);

  /**
   * Activate a tab, un-hiding it first if the user had hidden it.
   *
   * Every PROGRAMMATIC switch goes through here (chat jump, notification click,
   * artifacts auto-follow). Activating a hidden tab would render a panel body
   * whose tab is absent from the strip, leaving the strip with no selection at
   * all. An explicit jump is a strong enough signal of intent that re-showing
   * the tab is the right resolution — silently doing nothing would be worse.
   * User clicks from the strip always name a visible tab, so this is a no-op
   * on that path.
   */
  const activateRightTab = useCallback(
    (tab: RightTab) => {
      if (rightTabsHiddenRef.current.includes(tab)) {
        const next = rightTabsHiddenRef.current.filter((t) => t !== tab);
        rightTabsHiddenRef.current = next;
        setRightTabsHiddenState(next);
        persistPanelPrefs();
      }
      setRightTabState(tab);
    },
    [persistPanelPrefs],
  );

  const flashArtifacts = useCallback((ids: string[]) => {
    if (!ids.length) return;
    setFlashArtifactIds(ids);
    window.setTimeout(() => setFlashArtifactIds([]), 2500);
  }, []);

  return {
    rightTab,
    artifactsFollow,
    setArtifactsFollow,
    flashArtifactIds,
    setFlashArtifactIds,
    rightPanelOpen,
    rightPanelWidth,
    rightPanelWidthByTab,
    panelBadge,
    setPanelBadge,
    rightTabOrder,
    rightTabsHidden,
    visibleRightTabs,
    setRightPanelOpen,
    setRightPanelWidth,
    clearPanelBadge,
    setRightTabOrder,
    setRightTabHidden,
    resetRightTabs,
    activateRightTab,
    setRightTabState,
    rightPanelOpenRef,
    rightPanelWidthByTabRef,
    rightTabRef,
    artifactsFollowRef,
    rightTabsHiddenRef,
    pendingFlashRef,
    flashArtifacts,
  };
}
