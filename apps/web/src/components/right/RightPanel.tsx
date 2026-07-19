"use client";

import { useState } from "react";
import { TodoPanel } from "./TodoPanel";
import { WorkflowPanel } from "./WorkflowPanel";
import { ArtifactsPanel } from "./ArtifactsPanel";

type Tab = "todo" | "workflow" | "artifacts";

const TABS: { id: Tab; label: string }[] = [
  { id: "todo", label: "TODO" },
  { id: "workflow", label: "Workflow" },
  { id: "artifacts", label: "Artifacts" },
];

export function RightPanel() {
  const [tab, setTab] = useState<Tab>("todo");
  return (
    <aside className="flex w-[380px] shrink-0 flex-col border-l border-line bg-panel">
      <div className="flex gap-1 border-b border-line px-3 py-3">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
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
      </div>
    </aside>
  );
}
