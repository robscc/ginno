"use client";

import { FileText, Workflow, Link2, Boxes } from "lucide-react";
import { useGinno } from "@/lib/store";

const ICON: Record<string, typeof FileText> = { workflow: Workflow, link: Link2, doc: FileText };

export function ArtifactsPanel() {
  const g = useGinno();
  const items = g.artifacts;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Boxes className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">Artifacts</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{items.length}</span>
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto px-3">
        {items.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">
            No artifacts yet. Files / docs / workflows you produce or attach show up here.
          </div>
        )}
        {items.map((a) => {
          const Ic = ICON[a.kind] || FileText;
          return (
            <div key={a.id} className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm hover:bg-card/50">
              <Ic className="h-4 w-4 shrink-0 text-violet" />
              <span className="truncate text-txt">{a.name}</span>
              {a.ref && <span className="truncate text-xs text-faint">{a.ref}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
