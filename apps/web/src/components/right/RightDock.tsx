"use client";

import { useEffect, useRef, useState } from "react";
import { useGinno, type RightTab } from "@/lib/store";
import { RIGHT_TABS } from "./RightPanel";

/**
 * Collapsed-state affordance for the right panel (right-panel-redesign.md
 * §3.2). Renders as a 6px strip at the workspace's right edge:
 *
 *  - a resting hint bar (clickable — the touch/no-hover fallback) with a
 *    violet dot when unread artifacts queued up while collapsed;
 *  - hovering the strip slides a dock pill out over the chat: one icon per
 *    panel (same order/icons as the tab bar), with unread badges. Clicking an
 *    icon expands the panel straight onto that tab.
 *
 * The strip is a real layout element (not an overlay), so it never covers the
 * chat scrollbar. The pill is absolutely positioned — sliding it in/out never
 * reflows the chat.
 */
export function RightDock() {
  const g = useGinno();
  const [hover, setHover] = useState(false);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (leaveTimer.current) clearTimeout(leaveTimer.current);
    };
  }, []);

  const onEnter = () => {
    if (leaveTimer.current) clearTimeout(leaveTimer.current);
    setHover(true);
  };
  // 250ms grace so sweeping the mouse across the edge doesn't flicker.
  const onLeave = () => {
    if (leaveTimer.current) clearTimeout(leaveTimer.current);
    leaveTimer.current = setTimeout(() => setHover(false), 250);
  };

  const openTab = (tab: RightTab) => {
    g.setRightTab(tab, { manual: true });
    g.setRightPanelOpen(true); // also consumes all badges
  };

  const unreadArtifacts = g.panelBadge.artifacts ?? 0;

  return (
    <div
      className="relative w-1.5 shrink-0"
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      {/* resting hint bar — vertically centered on the edge */}
      <button
        onClick={() => openTab(g.rightTab)}
        aria-label="展开右侧面板"
        title="展开面板（⌘\ / Ctrl+\）"
        className="group absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full p-1"
      >
        <span className="block h-8 w-[3px] rounded-full bg-line2 transition-colors group-hover:bg-violet" />
        {unreadArtifacts > 0 && (
          <span className="absolute -left-0.5 -top-0.5 h-2 w-2 animate-pulse rounded-full bg-violet" />
        )}
      </button>

      {/* hover dock — slides out over the chat, anchored to the strip */}
      <div
        role="toolbar"
        aria-label="右栏面板"
        aria-hidden={!hover}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        className={`absolute right-full top-1/2 mr-1.5 flex -translate-y-1/2 flex-col gap-0.5 rounded-xl border border-line bg-panel p-1 shadow-2xl transition-all duration-150 ease-out ${
          hover ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-2 opacity-0"
        }`}
      >
        {RIGHT_TABS.map((t) => {
          const Ic = t.icon;
          const n = t.id === "artifacts" ? unreadArtifacts : 0;
          // Mirror the tab-bar workflow badges so the collapsed dock carries
          // the same signal (work item E).
          const showActive = t.id === "workflow" && g.activeRunCount > 0;
          const showFailed = t.id === "workflow" && g.unseenFailedCount > 0;
          const badgeExtra = n
            ? `，${n} 个新文件`
            : showActive || showFailed
              ? `，${g.activeRunCount} 个运行中，${g.unseenFailedCount} 个新失败`
              : "";
          return (
            <button
              key={t.id}
              onClick={() => openTab(t.id)}
              tabIndex={hover ? 0 : -1}
              aria-label={`${t.label}${badgeExtra}`}
              title={`${t.label}${badgeExtra}`}
              className="relative rounded-lg p-2 text-muted transition-colors hover:bg-card2 hover:text-txt"
            >
              <Ic className="h-[18px] w-[18px]" />
              {n > 0 && (
                <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-violet px-1 text-[10px] font-semibold leading-none text-white">
                  {n > 99 ? "99+" : n}
                </span>
              )}
              {showActive && (
                <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] animate-pulse items-center justify-center rounded-full bg-blue px-1 text-[10px] font-semibold leading-none text-white">
                  {g.activeRunCount}
                </span>
              )}
              {showFailed && (
                <span
                  className={`absolute -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-red px-1 text-[10px] font-semibold leading-none text-white ${
                    showActive ? "-right-4" : "-right-0.5"
                  }`}
                >
                  {g.unseenFailedCount}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
