"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno, type RightTab } from "@/lib/store";
import { RIGHT_TAB_BY_ID } from "@/lib/rightTabs";

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
  // i18n：t 面板/dock 框架文案，tb 角标文案，tn 右栏名与 tab 名
  const t = useTranslations("right.dock");
  const tb = useTranslations("right.badges");
  const tn = useTranslations("right");
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
        aria-label={t("expand")}
        title={t("expandTitle")}
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
        aria-label={tn("name")}
        aria-hidden={!hover}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        className={`absolute right-full top-1/2 mr-1.5 flex -translate-y-1/2 flex-col gap-0.5 rounded-xl border border-line bg-panel p-1 shadow-2xl transition-all duration-150 ease-out ${
          hover ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-2 opacity-0"
        }`}
      >
        {g.visibleRightTabs.map((id) => {
          const meta = RIGHT_TAB_BY_ID[id];
          const Ic = meta.icon;
          const n = meta.id === "artifacts" ? unreadArtifacts : 0;
          // Mirror the tab-bar workflow badges so the collapsed dock carries
          // the same signal (work item E). The yellow "needs your input" badge
          // is the strongest signal and claims the first slot (P1); each
          // following badge shifts 18px left.
          const wfBadges: Array<{ count: number; cls: string }> = [];
          if (meta.id === "workflow") {
            if (g.pendingHumanCount > 0)
              wfBadges.push({ count: g.pendingHumanCount, cls: "bg-yellow text-black animate-pulse" });
            if (g.activeRunCount > 0)
              wfBadges.push({ count: g.activeRunCount, cls: "bg-blue text-white animate-pulse" });
            if (g.unseenFailedCount > 0)
              wfBadges.push({ count: g.unseenFailedCount, cls: "bg-red text-white" });
          }
          // 总结 tab: blue pulse dot while a synthesis is in flight (mirrors
          // the tab bar).
          const synthActive = meta.id === "synthesis" && g.synthesisActiveCount > 0;
          // Memory tab: violet pulse dot while a draft awaits review.
          const memoryDraft = meta.id === "memory" && (g.panelBadge.memory ?? 0) > 0;
          const badgeExtra = n
            ? `, ${tb("newFiles", { n })}`
            : wfBadges.length
              ? `, ${tb("workflow", {
                  human: g.pendingHumanCount,
                  active: g.activeRunCount,
                  failed: g.unseenFailedCount,
                })}`
              : synthActive
                ? `, ${tb("synthesis", { n: g.synthesisActiveCount })}`
                : memoryDraft
                  ? `, ${tb("memoryDraft")}`
                  : "";
          return (
            <button
              key={meta.id}
              onClick={() => openTab(meta.id)}
              tabIndex={hover ? 0 : -1}
              aria-label={`${tn(`tabs.${meta.id}`)}${badgeExtra}`}
              title={`${tn(`tabs.${meta.id}`)}${badgeExtra}`}
              className="relative rounded-lg p-2 text-muted transition-colors hover:bg-card2 hover:text-txt"
            >
              <Ic className="h-[18px] w-[18px]" />
              {n > 0 && (
                <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-violet px-1 text-[10px] font-semibold leading-none text-white">
                  {n > 99 ? "99+" : n}
                </span>
              )}
              {wfBadges.map((b, i) => (
                <span
                  key={i}
                  className={`absolute -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full px-1 text-[10px] font-semibold leading-none ${b.cls}`}
                  style={{ right: `${-2 - i * 18}px` }}
                >
                  {b.count > 99 ? "99+" : b.count}
                </span>
              ))}
              {synthActive && (
                <span className="absolute -right-0.5 -top-0.5 inline-block h-2 w-2 animate-pulse rounded-full bg-blue" />
              )}
              {memoryDraft && (
                <span className="absolute -right-0.5 -top-0.5 inline-block h-2 w-2 animate-pulse rounded-full bg-violet" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
