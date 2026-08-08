"use client";

import { useEffect, useState } from "react";
import { Brain, FileBox, ListTodo, PanelRightClose, Zap, type LucideIcon } from "lucide-react";
import { PANEL_WIDTH_DEFAULT, useGinno, type RightTab } from "@/lib/store";
import { TodoPanel } from "./TodoPanel";
import { WorkflowPanel } from "./WorkflowPanel";
import { ArtifactsPanel } from "./ArtifactsPanel";
import { MemoryPanel } from "./MemoryPanel";

// Tab order follows right-panel-redesign.md §3.1: Artifacts first (highest
// frequency), default tab included. Icons are shared with the collapsed
// RightDock so both affordances read identically.
export const RIGHT_TABS: { id: RightTab; label: string; icon: LucideIcon }[] = [
  { id: "artifacts", label: "Artifacts", icon: FileBox },
  { id: "todo", label: "TODO", icon: ListTodo },
  { id: "workflow", label: "Workflow", icon: Zap },
  { id: "memory", label: "Memory", icon: Brain },
];

export function RightPanel() {
  const g = useGinno();
  const tab = g.rightTab;
  const width = g.rightPanelWidth;
  // Below the default width the labels don't fit beside the icons — drop to
  // icon-only tabs (titles keep them discoverable).
  const compact = width < PANEL_WIDTH_DEFAULT;

  // Drag-to-resize on the left edge. Width lives in the store (clamped +
  // persisted there); the drag loop only reports pointer positions.
  const [dragging, setDragging] = useState(false);
  // Stable callback from the store — kept out of `g` so the effect doesn't
  // re-arm on every provider render.
  const setRightPanelWidth = g.setRightPanelWidth;
  useEffect(() => {
    if (!dragging) return;
    document.body.style.cursor = "col-resize";
    document.body.classList.add("select-none");
    const onMove = (e: MouseEvent) => {
      // Panel's right edge is flush with the window's right edge.
      setRightPanelWidth(window.innerWidth - e.clientX);
    };
    const onUp = () => setDragging(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      document.body.style.cursor = "";
      document.body.classList.remove("select-none");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, setRightPanelWidth]);

  return (
    <aside
      className="relative flex shrink-0 flex-col border-l border-line bg-panel"
      style={{ width }}
    >
      {/* resize handle — drag to change width, double-click to reset */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="拖拽调整面板宽度（双击重置）"
        onMouseDown={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDoubleClick={() => g.setRightPanelWidth(PANEL_WIDTH_DEFAULT)}
        className={`absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize transition-colors ${
          dragging ? "bg-violet/60" : "hover:bg-violet/40"
        }`}
      />
      <div className="flex items-center gap-0.5 border-b border-line px-2 py-2.5">
        <div role="tablist" aria-label="右栏面板" className="flex min-w-0 flex-1 items-center gap-0.5">
          {RIGHT_TABS.map((t) => {
            const Ic = t.icon;
            // Workflow tab badge (work item E): blue = active runs (pulsing),
            // red = failures the user hasn't looked at yet.
            const showActive = t.id === "workflow" && g.activeRunCount > 0;
            const showFailed = t.id === "workflow" && g.unseenFailedCount > 0;
            const ariaExtra =
              t.id === "workflow" && (showActive || showFailed)
                ? `，${g.activeRunCount} 个运行中，${g.unseenFailedCount} 个新失败`
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
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "todo" && <TodoPanel />}
        {tab === "workflow" && <WorkflowPanel />}
        {tab === "artifacts" && <ArtifactsPanel />}
        {tab === "memory" && <MemoryPanel />}
      </div>
    </aside>
  );
}
