"use client";

// AgentChips — composer 上方的 @agent 选择 chips（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）。「推荐」标签由草稿关键词派生（render 期，同 sel 模式）。

import { useTranslations } from "next-intl";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";
import type { useGinno } from "@/lib/store";
import type { SessionMeta } from "@/lib/types";
import { recommendAgentId } from "../streamCore";

export function AgentChips({
  g,
  session,
  target,
  input,
  onSelect,
}: {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  target: string | null;
  input: string;
  onSelect: (id: string | null) => void;
}) {
  const tc = useTranslations("chat");
  const recAgentId = recommendAgentId(input, new Set(g.agents.map((a) => a.id)));
  return (
    <>
      {g.agents.map((a) => {
        const sel = (target ?? session?.agent_id) === a.id;
        const hex = agentHex(a.color);
        const rec = recAgentId === a.id && !sel;
        return (
          <button
            key={a.id}
            onClick={() => onSelect(sel ? null : a.id)}
            className={
              sel
                ? "flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition-colors"
                : "flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt"
            }
            style={sel ? { borderColor: hex, background: hex + "1a", color: hex } : undefined}
          >
            <Icon name={a.icon} className="h-3.5 w-3.5" />
            {tc("agentChips.ask", { name: a.name })}
            {rec && (
              <span className="rounded-full border border-yellow/40 bg-yellow/10 px-1.5 text-[10px] leading-4 text-yellow">
                {tc("agentChips.recommended")}
              </span>
            )}
          </button>
        );
      })}
    </>
  );
}
