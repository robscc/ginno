"use client";

import { useTranslations } from "next-intl";
import { SettingsNav } from "./SettingsNav";
import { SettingsTabs } from "./SettingsTabs";
import { defaultTab, getGroup, isValidSub } from "./settingsGroups";
import { ModelApiSettings } from "./ModelApiSettings";
import { SkillsSettings } from "./SkillsSettings";
import { McpSettings } from "./McpSettings";
import { ModsSettings } from "./ModsSettings";
import { AgentsSettings } from "./AgentsSettings";
import { WorkflowsSettings } from "./WorkflowsSettings";
import { GeneralSettings } from "./GeneralSettings";
import { NotificationsSettings } from "./NotificationsSettings";
import { FloatingSettings } from "./FloatingSettings";
import { KnowledgeSettings } from "./KnowledgeSettings";
import { ContextFoldersSettings } from "./ContextFoldersSettings";
import { WebSearchSettings } from "./WebSearchSettings";
import { ExternalAgentsSettings } from "./ExternalAgentsSettings";
import { PermissionsSettings } from "./PermissionsSettings";
import { HooksSettings } from "./HooksSettings";
import { SessionFilesSettings } from "./SessionFilesSettings";
import { UsageSettings } from "./UsageSettings";
import { ToolLabelsSettings } from "./ToolLabelsSettings";
import { SynthesisQualitySettings } from "./SynthesisQualitySettings";

// 按 (group, sub) 渲染对应面板；sub 缺省取分组默认 tab，非法 sub 走 unknownTab 兜底。
function TabPanel({ tab }: { tab: string }) {
  switch (tab) {
    case "model-api":
      return <ModelApiSettings />;
    case "mcp":
      return <McpSettings />;
    case "mods":
      return <ModsSettings />;
    case "web":
      return <WebSearchSettings />;
    case "external-agents":
      return <ExternalAgentsSettings />;
    case "agents":
      return <AgentsSettings />;
    case "workflows":
      return <WorkflowsSettings />;
    case "skills":
      return <SkillsSettings />;
    case "knowledge":
      return <KnowledgeSettings />;
    case "folders":
      return <ContextFoldersSettings />;
    case "permissions":
      return <PermissionsSettings />;
    case "hooks":
      return <HooksSettings />;
    case "usage":
      return <UsageSettings />;
    case "session-files":
      return <SessionFilesSettings />;
    case "synthesis-quality":
      return <SynthesisQualitySettings />;
    default:
      return null;
  }
}

export function SettingsView({ group, sub }: { group: string; sub?: string }) {
  const t = useTranslations("settings.view");
  const g = getGroup(group);
  return (
    <div className="flex min-w-0 flex-1">
      <SettingsNav active={group} />
      <div className="min-w-0 flex-1 overflow-y-auto">
        {!g ? (
          <div className="px-8 py-10 text-sm text-faint">{t("unknownTab", { tab: group })}</div>
        ) : g.id === "general" ? (
          // 单页分组：旧 notifications / floating / tool-labels 锚点在此落地
          <div>
            <GeneralSettings />
            <div id="notifications" className="border-t border-line">
              <NotificationsSettings />
            </div>
            <div id="floating" className="border-t border-line">
              <FloatingSettings />
            </div>
            <div id="tool-labels" className="border-t border-line">
              <ToolLabelsSettings />
            </div>
          </div>
        ) : !isValidSub(g, sub ?? defaultTab(g)) ? (
          <div className="px-8 py-10 text-sm text-faint">{t("unknownTab", { tab: sub ?? group })}</div>
        ) : (
          <>
            <SettingsTabs group={g} active={sub ?? defaultTab(g)} />
            <TabPanel tab={sub ?? defaultTab(g)} />
          </>
        )}
      </div>
    </div>
  );
}
