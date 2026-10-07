"use client";

import { useState } from "react";
import { MoreVertical } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { AgentConfig, SessionMeta, SessionUsage } from "@/lib/types";
import { agentHex } from "@/lib/theme";
import { AgentIcon } from "@/components/icons";
import { GoalChip } from "./GoalChip";
import { ContextFoldersChip } from "./ContextFoldersChip";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Click-to-copy session uuid chip — same shape as the per-turn trace chip on
 * chat bubbles. The full id is what you grep the sidecar logs for
 * (`session=...`); we show a short prefix to keep the bar tidy. */
function SessionIdChip({ sessionId }: { sessionId?: string }) {
  const tr = useTranslations("shell");
  const [copied, setCopied] = useState(false);
  if (!sessionId) return null;
  const short = sessionId.slice(0, 8);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(sessionId);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          /* clipboard unavailable */
        }
      }}
      title={tr("topbar.idChipTitle", { id: sessionId })}
      className="shrink-0 whitespace-nowrap rounded border border-line2 px-1 py-px font-mono text-[9px] text-faint transition-colors hover:border-violet/50 hover:text-violet"
    >
      {copied ? tr("topbar.copied") : `#${short}`}
    </button>
  );
}

export function TopBar({
  session,
  agent,
  running,
  usage,
}: {
  session: SessionMeta | null;
  agent: AgentConfig | null;
  running: boolean;
  usage?: SessionUsage | null;
}) {
  const g = useGinno();
  const tr = useTranslations("shell");
  const [menu, setMenu] = useState(false);
  const hex = agentHex(agent?.color);

  return (
    <header className="flex min-h-14 min-w-0 shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line px-5 py-1.5">
      <div className="flex min-w-[min(100%,12rem)] flex-1 flex-wrap items-center gap-x-2.5 gap-y-1">
        <h1 className="min-w-[5rem] max-w-full truncate text-[15px] font-semibold tracking-tight text-txt" title={session?.title || tr("topbar.newSession")}>
          {session?.title || tr("topbar.newSession")}
        </h1>
        <SessionIdChip sessionId={session?.id} />

        {agent && (
          <span className="pill border border-line2 bg-card text-txt">
            <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center" style={{ color: hex }}>
              <AgentIcon name={agent.icon} className="h-3.5 w-3.5" />
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
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: running ? "#22c55e" : "#71717a" }}
          />
          {running ? tr("topbar.running") : tr("topbar.idle")}
        </span>

        <GoalChip sessionId={session?.id ?? null} />

        <ContextFoldersChip session={session} />
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-2">
        {usage && usage.calls > 0 && (
          <span
            className="pill font-mono text-[11px]"
            style={{ background: "#3b82f61a", color: "#93c5fd" }}
            title={tr("topbar.usageTitle", {
              input: usage.input_tokens,
              cache: usage.cache_read_tokens,
              output: usage.output_tokens,
              calls: usage.calls,
            })}
          >
            ↑{fmtTokens(usage.input_tokens)} ↓{fmtTokens(usage.output_tokens)}
            {usage.cache_read_tokens > 0 && (
              <span style={{ color: "#4ade80" }}> ⚡{Math.round((usage.cache_read_tokens / Math.max(1, usage.input_tokens)) * 100)}%</span>
            )}
          </span>
        )}
        <div className="relative">
          <button
            onClick={() => setMenu((m) => !m)}
            aria-label={tr("topbar.actions")}
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
                    const name = window.prompt(tr("topbar.renamePrompt"), session.title || "");
                    if (name != null && name.trim()) {
                      await api.patchSession(session.id, { title: name.trim() });
                      await g.reloadSessions();
                    }
                  }}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                >
                  {tr("session.rename")}
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(false);
                    if (session) void navigator.clipboard?.writeText(session.id);
                  }}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                >
                  {tr("topbar.copyId")}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
