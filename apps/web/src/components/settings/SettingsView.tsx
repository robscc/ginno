"use client";

import { SettingsNav } from "./SettingsNav";
import { ModelApiSettings } from "./ModelApiSettings";

const LABELS: Record<string, string> = {
  skills: "Skills",
  mcp: "MCP 工具",
  agents: "Agent 管理",
  workflows: "Workflows",
  general: "通用设置",
  notifications: "通知",
};

export function SettingsView({ tab }: { tab: string }) {
  return (
    <div className="flex min-w-0 flex-1">
      <SettingsNav active={tab} />
      <div className="min-w-0 flex-1 overflow-y-auto">
        {tab === "model-api" ? (
          <ModelApiSettings />
        ) : (
          <div className="px-8 py-10">
            <h2 className="text-lg font-semibold text-txt">{LABELS[tab] || tab}</h2>
            <p className="mt-1 text-sm text-faint">即将推出（Phase F）。</p>
          </div>
        )}
      </div>
    </div>
  );
}
