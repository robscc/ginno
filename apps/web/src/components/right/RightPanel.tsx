"use client";

import { useEffect, useRef } from "react";
import { PanelRightClose } from "lucide-react";
import { PANEL_WIDTH_DEFAULT, useGinno } from "@/lib/store";
import { RIGHT_TAB_BY_ID } from "@/lib/rightTabs";
import { PanelResizer } from "@/components/workflow/studio/PanelResizer";
import { CodePanel } from "./code/CodePanel";
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
        ariaLabel="拖拽调整面板宽度（双击重置）"
        onDrag={(d) => {
          liveWidthRef.current -= d;
          setRightPanelWidth(liveWidthRef.current);
        }}
        onReset={() => setRightPanelWidth(PANEL_WIDTH_DEFAULT)}
      />
      <aside className="relative flex shrink-0 flex-col border-l border-line bg-panel" style={{ width }}>
        <div className="flex items-center gap-0.5 border-b border-line px-2 py-2.5">
          <div role="tablist" aria-label="右栏面板" className="flex min-w-0 flex-1 items-center gap-0.5">
            {g.visibleRightTabs.map((id) => {
              const t = RIGHT_TAB_BY_ID[id];
              const Ic = t.icon;
              // Workflow tab badge (work item E): yellow = waiting for a human
              // answer (strongest signal, P1), blue = active runs (pulsing),
              // red = failures the user hasn't looked at yet.
              const showHuman = t.id === "workflow" && g.pendingHumanCount > 0;
              const showActive = t.id === "workflow" && g.activeRunCount > 0;
              const showFailed = t.id === "workflow" && g.unseenFailedCount > 0;
              // 总结 tab: blue pulse dot while a synthesis case is in flight.
              const showSynthActive = t.id === "synthesis" && g.synthesisActiveCount > 0;
              // Memory tab: violet pulse dot while a distillation draft awaits
              // review (memory.changed WS event drives the badge).
              const showMemoryDraft = t.id === "memory" && (g.panelBadge.memory ?? 0) > 0;
              const ariaExtra =
                t.id === "workflow" && (showHuman || showActive || showFailed)
                  ? `，${g.pendingHumanCount} 个等待输入，${g.activeRunCount} 个运行中，${g.unseenFailedCount} 个新失败`
                  : showSynthActive
                    ? `，${g.synthesisActiveCount} 个总结进行中`
                    : showMemoryDraft
                      ? "，有记忆草稿待审核"
                      : "";
              return (
                <button
                  key={t.id}
                  role="tab"
                  aria-selected={tab === t.id}
                  aria-label={`${t.label}${ariaExtra}`}
                  title={t.label}
                  onClick={() => g.setRightTab(t.id, { manual: true })}
                  className={`flex items-center rounded-lg px-2 py-1.5 text-[13px] font-medium transition-colors ${
                    tab === t.id ? "bg-card2 text-txt" : "text-muted hover:text-txt"
                  }`}
                >
                  <Ic className="h-3.5 w-3.5 shrink-0" />
                  {!compact && <span className="ml-1 truncate">{t.label}</span>}
                  {showHuman && (
                    <span
                      className="ml-1 inline-flex h-4 min-w-[16px] animate-pulse items-center justify-center rounded-full bg-yellow px-1 text-[10px] font-semibold leading-none text-black"
                      title={`${g.pendingHumanCount} 个运行等待你的输入`}
                    >
                      {g.pendingHumanCount}
                    </span>
                  )}
                  {showActive && (
                    <span
                      className="ml-1 inline-flex h-4 min-w-[16px] animate-pulse items-center justify-center rounded-full bg-blue px-1 text-[10px] font-semibold leading-none text-white"
                      title={`${g.activeRunCount} 个运行正在运行/暂停`}
                    >
                      {g.activeRunCount}
                    </span>
                  )}
                  {showFailed && (
                    <span
                      className="ml-1 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-red px-1 text-[10px] font-semibold leading-none text-white"
                      title={`${g.unseenFailedCount} 个运行失败，点击查看`}
                    >
                      {g.unseenFailedCount}
                    </span>
                  )}
                  {showSynthActive && (
                    <span
                      className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-blue"
                      title={`${g.synthesisActiveCount} 个总结进行中`}
                    />
                  )}
                  {showMemoryDraft && (
                    <span
                      className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-violet"
                      title="有记忆草稿待审核"
                    />
                  )}
                </button>
              );
            })}
          </div>
          <button
            onClick={() => g.setRightPanelOpen(false)}
            aria-label="收起面板"
            title="收起面板（⌘\ / Ctrl+\）"
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-card hover:text-txt"
          >
            <PanelRightClose className="h-4 w-4" />
          </button>
        </div>
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