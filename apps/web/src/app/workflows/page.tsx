"use client";

import { StudioShell } from "@/components/workflow/studio/StudioShell";

/** Workflow Studio (design B · 工作室优先): the workflow is the primary object.
 *  Three panes — recipes+runs / canvas·observer·versions / inspector — with the
 *  selection mirrored into the URL hash so the view survives a reload.
 *
 *  Static-export friendly: one route, all state client-side. */
export default function WorkflowsPage() {
  // NOTE: AppShell renders non-workspace routes inside a `flex` item, so this
  // root must claim the flex space itself (a plain h-full div would be sized
  // to its content and squash the canvas).
  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      <StudioShell />
    </div>
  );
}