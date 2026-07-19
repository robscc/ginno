"use client";

import { useRouter } from "next/navigation";
import { MoreVertical, Globe } from "lucide-react";
import type { AgentConfig, SessionMeta } from "@/lib/types";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";

export function TopBar({
  session,
  agent,
  running,
  modelLabel,
}: {
  session: SessionMeta | null;
  agent: AgentConfig | null;
  running: boolean;
  modelLabel: string;
}) {
  const router = useRouter();
  const hex = agentHex(agent?.color);

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line px-5">
      <h1 className="text-[15px] font-semibold tracking-tight text-txt">
        {session?.title || "New Session"}
      </h1>

      {agent && (
        <span className="pill border border-line2 bg-card text-txt">
          <span className="flex h-3.5 w-3.5 items-center justify-center" style={{ color: hex }}>
            <Icon name={agent.icon} className="h-3.5 w-3.5" />
          </span>
          {agent.name}
        </span>
      )}

      <span
        className="pill"
        style={{
          background: running ? "#22c55e22" : "#52525b22",
          color: running ? "#4ade80" : "#a1a1aa",
        }}
      >
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ background: running ? "#22c55e" : "#71717a" }}
        />
        {running ? "Running" : "Idle"}
      </span>

      <div className="ml-auto flex items-center gap-2">
        <button
          onClick={() => router.push("/settings/model-api")}
          className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-muted hover:bg-card hover:text-txt"
          title="Model settings"
        >
          <Globe className="h-3.5 w-3.5 text-muted" />
          {modelLabel || "model"}
        </button>
        <button className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt">
          <MoreVertical className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}
