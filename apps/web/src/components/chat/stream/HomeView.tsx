"use client";

// HomeView — 无会话时的落地页（自 ChatStream.tsx 机械拆出，渲染逐字一致）。

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { greeting } from "@/lib/utils";
import { Icon } from "@/components/icons";
import type { useGinno } from "@/lib/store";
import { FirstRunCards } from "../FirstRunCards";

export function HomeView({
  agents,
  target,
  agentChips,
  composer,
  showFirstRun,
  firstRunBusy,
  onMountFolder,
  onAsk,
  onWorkflow,
  onDismiss,
  onChipFill,
  onOpenGoal,
}: {
  agents: ReturnType<typeof useGinno>["agents"];
  target: string | null;
  agentChips: ReactNode;
  composer: ReactNode;
  showFirstRun: boolean;
  firstRunBusy: boolean;
  onMountFolder: () => void;
  onAsk: () => void;
  onWorkflow: () => void;
  onDismiss: () => void;
  onChipFill: (fill: string) => void;
  onOpenGoal?: () => void;
}) {
  const tc = useTranslations("chat");
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-6 pb-10 pt-6">
      <Icon name="star" className="h-48 w-48" style={{ color: "rgba(233,233,240,0.05)" }} />
      <div className="mt-2 text-center text-[26px] font-semibold tracking-tight text-txt">
        {greeting()}
      </div>
      <div className="mt-2 text-center text-[13px] text-faint">
        {tc("home.tagline")}
      </div>
      {showFirstRun && (
        <div className="mt-6 w-full max-w-[760px]">
          <FirstRunCards
            busy={firstRunBusy}
            onMountFolder={onMountFolder}
            onAsk={onAsk}
            onWorkflow={onWorkflow}
            onDismiss={onDismiss}
          />
        </div>
      )}
      <div key="composer-home" className="mt-8 w-full max-w-[760px]">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          {agentChips}
          {!target && (
            <span className="ml-auto self-center text-[11px] text-faint">
              {tc("home.defaultAgentHint", { name: agents[0]?.name ?? tc("stream.fallbackAgent") })}
            </span>
          )}
        </div>
        {composer}
      </div>
      <div className="mt-6 flex max-w-[820px] flex-wrap justify-center gap-2.5">
        {[
          { icon: "📊", label: tc("home.chipExcelLabel"), fill: tc("home.chipExcelFill") },
          { icon: "🔁", label: tc("home.chipWorkflowLabel"), fill: tc("home.chipWorkflowFill") },
          { icon: "🧠", label: tc("home.chipMemoryLabel"), fill: tc("home.chipMemoryFill") },
        ].map((c) => (
          <button
            key={c.label}
            onClick={() => onChipFill(c.fill)}
            className="inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-[12.5px] text-muted transition-colors hover:border-line2 hover:bg-card hover:text-txt"
          >
            <span>{c.icon}</span>
            {c.label}
          </button>
        ))}
        <button
          onClick={() => onOpenGoal?.()}
          className="inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-[12.5px] text-muted transition-colors hover:border-line2 hover:bg-card hover:text-txt"
        >
          <span>🎯</span>
          {tc("home.setGoal")}
        </button>
      </div>
      <div className="mt-6 text-[11px] text-faint">
        {tc("home.hintLine")}
      </div>
    </div>
  );
}
