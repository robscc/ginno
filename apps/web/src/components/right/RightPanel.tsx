"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { PanelRightClose } from "lucide-react";
import { PANEL_WIDTH_DEFAULT, useGinno } from "@/lib/store";
import { RIGHT_TAB_BY_ID } from "@/lib/rightTabs";
import { PanelResizer } from "@/components/workflow/studio/PanelResizer";
import { CodePanel } from "./code/CodePanel";
import { ModPaneHost } from "./ModPaneHost";
import { TodoPanel } from "./TodoPanel";
import { WorkflowPanel } from "./WorkflowPanel";
import { ArtifactsPanel } from "./ArtifactsPanel";
import { MemoryPanel } from "./MemoryPanel";
import { SynthesisPanel } from "./SynthesisPanel";

// The tab registry — order, labels, icons — lives in `@/lib/rightTabs`: the
// store derives the visible list from it and Settings → 通用设置 reorders it.
// This strip and the collapsed dock both render `g.visibleRightTabs`, so a
// user's order/visibility choice applies to both affordances at once.

export function RightPanel() {
  const g = useGinno();
  // i18n：t 角标 title 等面板框架文案，tb 角标文案，tn 右栏名与 tab 名
  const t = useTranslations("right.panel");
  const tb = useTranslations("right.badges");
  const tn = useTranslations("right");
  const tab = g.rightTab;
  const width = g.rightPanelWidth;
  // Below the default width the labels don't fit beside the icons — drop to
  // icon-only tabs (titles keep them discoverable).
  const compact = width < PANEL_WIDTH_DEFAULT;

  // Width lives in the store (clamped per tab + persisted there); the resizer
  // only reports drag deltas. A live ref absorbs deltas between renders so a
  // burst of pointermove events can't apply one onto a stale width.
  const setRightPanelWidth = g.setRightPanelWidth;
  const liveWidthRef = useRef(width);
  useEffect(() => {
    liveWidthRef.current = width;
  }, [width]);

  return (
    <>
      {/* Left-edge resize handle (pointer capture, keyboard ±16px, double-click
          reset). Dragging left widens the panel, hence the negated delta. */}
      <PanelResizer
        ariaLabel={t("resize")}
        onDrag={(d) => {
          liveWidthRef.current -= d;
          setRightPanelWidth(liveWidthRef.current);
        }}
        onReset={() => setRightPanelWidth(PANEL_WIDTH_DEFAULT)}
      />
      <aside className="relative flex shrink-0 flex-col border-l border-line bg-panel" style={{ width }}>
        <div className="flex items-center gap-0.5 border-b border-line px-2 py-2.5">
          <div role="tablist" aria-label={tn("name")} className="flex min-w-0 flex-1 items-center gap-0.5">
            {g.visibleRightTabs.map((id) => {
              const meta = RIGHT_TAB_BY_ID[id];
              const Ic = meta.icon;
              // Workflow tab badge (work item E): yellow = waiting for a human
              // answer (strongest signal, P1), blue = active runs (pulsing),
              // red = failures the user hasn't looked at yet.
              const showHuman = meta.id === "workflow" && g.pendingHumanCount > 0;
              const showActive = meta.id === "workflow" && g.activeRunCount > 0;
              const showFailed = meta.id === "workflow" && g.unseenFailedCount > 0;
              // 总结 tab: blue pulse dot while a synthesis case is in flight.
              const showSynthActive = meta.id === "synthesis" && g.synthesisActiveCount > 0;
              // Memory tab: violet pulse dot while a distillation draft awaits
              // review (memory.changed WS event drives the badge).
              const showMemoryDraft = meta.id === "memory" && (g.panelBadge.memory ?? 0) > 0;
              const ariaExtra =
                meta.id === "workflow" && (showHuman || showActive || showFailed)
                  ? `, ${tb("workflow", {
                      human: g.pendingHumanCount,
                      active: g.activeRunCount,
                      failed: g.unseenFailedCount,
                    })}`
                  : showSynthActive
                    ? `, ${tb("synthesis", { n: g.synthesisActiveCount })}`
                    : showMemoryDraft
                      ? `, ${tb("memoryDraft")}`
                      : "";
              const label = tn(`tabs.${meta.id}`);
              return (
                <button
                  key={meta.id}
                  role="tab"
                  aria-selected={tab === meta.id}
                  aria-label={`${label}${ariaExtra}`}
                  title={label}
                  onClick={() => g.setRightTab(meta.id, { manual: true })}
                  className={`flex items-center rounded-lg px-2 py-1.5 text-[13px] font-medium transition-colors ${
                    tab === meta.id ? "bg-card2 text-txt" : "text-muted hover:text-txt"
                  }`}
                >
                  <Ic className="h-3.5 w-3.5 shrink-0" />
                  {!compact && <span className="ml-1 truncate">{label}</span>}
                  {showHuman && (
                    <span
                      className="ml-1 inline-flex h-4 min-w-[16px] animate-pulse items-center justify-center rounded-full bg-yellow px-1 text-[10px] font-semibold leading-none text-black"
                      title={t("waitingTitle", { n: g.pendingHumanCount })}
                    >
                      {g.pendingHumanCount}
                    </span>
                  )}
                  {showActive && (
                    <span
                      className="ml-1 inline-flex h-4 min-w-[16px] animate-pulse items-center justify-center rounded-full bg-blue px-1 text-[10px] font-semibold leading-none text-white"
                      title={t("runningTitle", { n: g.activeRunCount })}
                    >
                      {g.activeRunCount}
                    </span>
                  )}
                  {showFailed && (
                    <span
                      className="ml-1 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-red px-1 text-[10px] font-semibold leading-none text-white"
                      title={t("failedTitle", { n: g.unseenFailedCount })}
                    >
                      {g.unseenFailedCount}
                    </span>
                  )}
                  {showSynthActive && (
                    <span
                      className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-blue"
                      title={tb("synthesis", { n: g.synthesisActiveCount })}
                    />
                  )}
                  {showMemoryDraft && (
                    <span
                      className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-violet"
                      title={tb("memoryDraft")}
                    />
                  )}
                </button>
              );
            })}
          </div>
          <button
            onClick={() => g.setRightPanelOpen(false)}
            aria-label={t("collapse")}
            title={t("collapseTitle")}
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-card hover:text-txt"
          >
            <PanelRightClose className="h-4 w-4" />
          </button>
        </div>
        {/* Mods pane 区(§7.4):有 mod 打开 pane 时出现在 tab 内容之上,
            无 pane 时不占任何空间。 */}
        <ModPaneHost />
        {/* The code panel owns its own internal scrolling and fills the height;
            the other panels are plain vertical scrollers. */}
        <div className={tab === "code" ? "flex min-h-0 flex-1 flex-col overflow-hidden" : "min-h-0 flex-1 overflow-y-auto"}>
          {tab === "todo" && <TodoPanel />}
          {tab === "workflow" && <WorkflowPanel />}
          {tab === "artifacts" && <ArtifactsPanel />}
          {tab === "memory" && <MemoryPanel />}
          {tab === "synthesis" && <SynthesisPanel />}
          {tab === "code" && <CodePanel />}
        </div>
      </aside>
    </>
  );
}