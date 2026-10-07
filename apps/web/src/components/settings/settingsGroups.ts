import {
  BarChart3,
  BookOpen,
  Cpu,
  ShieldCheck,
  SlidersHorizontal,
  Users,
} from "lucide-react";

// Settings 分组的唯一权威契约：导航（SettingsNav）、子页签（SettingsTabs）、
// 路由 generateStaticParams、旧 URL 重定向全部由此派生，改分组只动这一个文件。
// labelKey / tab.label 存 settings.nav.* 的 catalog key（字面量联合，供 next-intl 类型检查）。

export type SettingsGroupId =
  | "connections"
  | "agents"
  | "knowledge"
  | "security"
  | "data"
  | "general";

export type SettingsTabKey =
  | "modelApi"
  | "mcp"
  | "webSearch"
  | "externalAgents"
  | "agents"
  | "workflows"
  | "skills"
  | "knowledge"
  | "folders"
  | "permissions"
  | "hooks"
  | "usage"
  | "sessionFiles"
  | "synthesis";

export type SettingsTab = { id: string; label: SettingsTabKey };
export type SettingsGroup = {
  id: SettingsGroupId;
  labelKey: SettingsGroupId; // 与 group id 同名，groups.* 的六个 key
  icon: typeof Cpu;
  color: string;
  tabs: SettingsTab[]; // general 为单页分组，tabs 为空
};

export const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    id: "connections",
    labelKey: "connections",
    icon: Cpu,
    color: "#a78bfa",
    tabs: [
      { id: "model-api", label: "modelApi" },
      { id: "mcp", label: "mcp" },
      { id: "web", label: "webSearch" },
      { id: "external-agents", label: "externalAgents" },
    ],
  },
  {
    id: "agents",
    labelKey: "agents",
    icon: Users,
    color: "#fb923c",
    tabs: [
      { id: "agents", label: "agents" },
      { id: "workflows", label: "workflows" },
      { id: "skills", label: "skills" },
    ],
  },
  {
    id: "knowledge",
    labelKey: "knowledge",
    icon: BookOpen,
    color: "#60a5fa",
    tabs: [
      { id: "knowledge", label: "knowledge" },
      { id: "folders", label: "folders" },
    ],
  },
  {
    id: "security",
    labelKey: "security",
    icon: ShieldCheck,
    color: "#f87171",
    tabs: [
      { id: "permissions", label: "permissions" },
      { id: "hooks", label: "hooks" },
    ],
  },
  {
    id: "data",
    labelKey: "data",
    icon: BarChart3,
    color: "#2dd4bf",
    tabs: [
      { id: "usage", label: "usage" },
      { id: "session-files", label: "sessionFiles" },
      { id: "synthesis-quality", label: "synthesis" },
    ],
  },
  {
    id: "general",
    labelKey: "general",
    icon: SlidersHorizontal,
    color: "#9ca3af",
    tabs: [],
  },
];

export function getGroup(id: string): SettingsGroup | undefined {
  return SETTINGS_GROUPS.find((g) => g.id === id);
}

export function defaultTab(group: SettingsGroup): string {
  return group.tabs[0]?.id ?? "";
}

export function isValidSub(group: SettingsGroup, sub: string): boolean {
  return group.tabs.some((tab) => tab.id === sub);
}

// 旧平铺 tab id → 新路径。静态导出（next.config.ts output: "export"）没有服务端
// redirect，只能在 /settings/[group] 页面级客户端跳转；旧 id 与新 group id 同名
// 且内容一致的（agents / knowledge / general）无需 redirect，故不在此列。
export const LEGACY_REDIRECT: Record<string, string> = {
  "model-api": "/settings/connections/model-api",
  mcp: "/settings/connections/mcp",
  web: "/settings/connections/web",
  "external-agents": "/settings/connections/external-agents",
  skills: "/settings/agents/skills",
  workflows: "/settings/agents/workflows",
  folders: "/settings/knowledge/folders",
  permissions: "/settings/security/permissions",
  hooks: "/settings/security/hooks",
  usage: "/settings/data/usage",
  "session-files": "/settings/data/session-files",
  "synthesis-quality": "/settings/data/synthesis-quality",
  notifications: "/settings/general#notifications",
  floating: "/settings/general#floating",
  "tool-labels": "/settings/general#tool-labels",
};
