"use client";

import { SettingsNav } from "./SettingsNav";
import { ModelApiSettings } from "./ModelApiSettings";
import { SkillsSettings } from "./SkillsSettings";
import { McpSettings } from "./McpSettings";
import { AgentsSettings } from "./AgentsSettings";
import { WorkflowsSettings } from "./WorkflowsSettings";
import { GeneralSettings } from "./GeneralSettings";
import { NotificationsSettings } from "./NotificationsSettings";
import { KnowledgeSettings } from "./KnowledgeSettings";
import { PermissionsSettings } from "./PermissionsSettings";
import { HooksSettings } from "./HooksSettings";
import { SessionFilesSettings } from "./SessionFilesSettings";

export function SettingsView({ tab }: { tab: string }) {
  return (
    <div className="flex min-w-0 flex-1">
      <SettingsNav active={tab} />
      <div className="min-w-0 flex-1 overflow-y-auto">
        {tab === "model-api" && <ModelApiSettings />}
        {tab === "skills" && <SkillsSettings />}
        {tab === "mcp" && <McpSettings />}
        {tab === "agents" && <AgentsSettings />}
        {tab === "workflows" && <WorkflowsSettings />}
        {tab === "knowledge" && <KnowledgeSettings />}
        {tab === "permissions" && <PermissionsSettings />}
        {tab === "hooks" && <HooksSettings />}
        {tab === "session-files" && <SessionFilesSettings />}
        {tab === "general" && <GeneralSettings />}
        {tab === "notifications" && <NotificationsSettings />}
        {!["model-api", "skills", "mcp", "agents", "workflows", "knowledge", "permissions", "hooks", "session-files", "general", "notifications"].includes(tab) && (
          <div className="px-8 py-10 text-sm text-faint">Unknown tab: {tab}</div>
        )}
      </div>
    </div>
  );
}
