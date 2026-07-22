"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { MoreVertical, Globe } from "lucide-react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
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
  const g = useGinno();
  const [menu, setMenu] = useState(false);
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
        <div className="relative">
          <button
            onClick={() => setMenu((m) => !m)}
            aria-label="会话操作"
            aria-haspopup="menu"
            aria-expanded={menu}
            className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt"
          >
            <MoreVertical className="h-4 w-4" />
          </button>
          {menu && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setMenu(false)} />
              <div
                role="menu"
                className="absolute right-0 z-50 mt-1 w-40 overflow-hidden rounded-lg border border-line bg-card py-1 text-sm shadow-xl"
              >
                <button
                  role="menuitem"
                  onClick={async () => {
                    setMenu(false);
                    if (!session) return;
                    const name = window.prompt("会话标题", session.title || "");
                    if (name != null && name.trim()) {
                      await api.patchSession(session.id, { title: name.trim() });
                      await g.reloadSessions();
                    }
                  }}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                >
                  重命名会话
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(false);
                    if (session) void navigator.clipboard?.writeText(session.id);
                  }}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                >
                  复制会话 ID
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
