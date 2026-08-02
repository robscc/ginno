"use client";

import { useGinno, type RightTab } from "@/lib/store";
import { TodoPanel } from "./TodoPanel";
import { WorkflowPanel } from "./WorkflowPanel";
import { ArtifactsPanel } from "./ArtifactsPanel";
import { MemoryPanel } from "./MemoryPanel";

const TABS: { id: RightTab; label: string }[] = [
  { id: "todo", label: "TODO" },
  { id: "workflow", label: "Workflow" },
  { id: "artifacts", label: "Artifacts" },
  { id: "memory", label: "Memory" },
];

export function RightPanel() {
  const g = useGinno();
  const tab = g.rightTab;
  return (
    <aside className="flex w-[380px] shrink-0 flex-col border-l border-line bg-panel">
      <div role="tablist" aria-label="右栏面板" className="flex gap-1 border-b border-line px-3 py-3">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => g.setRightTab(t.id, { manual: true })}
            className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
              tab === t.id ? "bg-card2 text-txt" : "text-muted hover:text-txt"
            }`}
          >
            {t.label}
          </button>
        ))}
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
