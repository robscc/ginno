"use client";

import { useEffect, useRef, useState } from "react";
import { useGinno } from "@/lib/store";
import { TopBar } from "@/components/shell/TopBar";
import { ChatStream } from "@/components/chat/ChatStream";
import { SheetViewer } from "@/components/chat/SheetViewer";
import { RightPanel } from "@/components/right/RightPanel";

export default function WorkspacePage() {
  const g = useGinno();
  const [running, setRunning] = useState(false);
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

  return (
    <div className="flex min-w-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar session={session} agent={agent} running={running} modelLabel={modelLabel} />
        <ChatStream session={session} onRunningChange={setRunning} />
      </div>
      <RightPanel />
      <SheetViewer />
    </div>
  );
}
