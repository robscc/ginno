"use client";

import { useEffect, useRef, useState } from "react";
import { useGinno } from "@/lib/store";
import { TopBar } from "@/components/shell/TopBar";
import { ChatStream } from "@/components/chat/ChatStream";
import { SheetViewer } from "@/components/chat/SheetViewer";
import { RightPanel } from "@/components/right/RightPanel";
import type { SessionUsage } from "@/lib/types";

export default function WorkspacePage() {
  const g = useGinno();
  const [running, setRunning] = useState(false);
  // Session-cumulative model usage (world-state-plan D2/D3), pushed up from
  // the chat socket and rendered as a small counter in the TopBar.
  const [usage, setUsage] = useState<SessionUsage | null>(null);
  const didInit = useRef(false);

  // pick / create an active session once data is loaded
  useEffect(() => {
    if (didInit.current) return;
    if (!g.ready) return; // wait until the first fetch of sessions+agents settled
    if (g.sessions.length) {
      if (!g.activeSessionId) g.setActiveSession(g.sessions[0].id);
      didInit.current = true;
    } else {
      didInit.current = true;
      g.newSession(g.agents[0]?.id);
    }
  }, [g.ready, g.sessions, g.agents, g.activeSessionId, g]);

  const session =
    g.sessions.find((s) => s.id === g.activeSessionId) ?? g.sessions[0] ?? null;
  const agent = session ? g.agents.find((a) => a.id === session.agent_id) ?? null : null;
  const modelLabel = session?.model || session?.provider || g.defaultProvider || "model";

  // Usage counters are per runtime-session; reset the display on session switch
  // (the next `usage` event re-populates it from the server's accumulator).
  useEffect(() => {
    setUsage(null);
  }, [g.activeSessionId]);

  return (
    <div className="flex min-w-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar session={session} agent={agent} running={running} modelLabel={modelLabel} usage={usage} />
        <ChatStream session={session} onRunningChange={setRunning} onUsageChange={setUsage} />
      </div>
      <RightPanel />
      <SheetViewer />
    </div>
  );
}
