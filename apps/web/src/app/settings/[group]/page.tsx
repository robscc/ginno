import { SettingsView } from "@/components/settings/SettingsView";
import { LegacySettingsRedirect } from "@/components/settings/LegacySettingsRedirect";
import { LEGACY_REDIRECT, SETTINGS_GROUPS, getGroup } from "@/components/settings/settingsGroups";

// 静态导出（next.config.ts output: "export"，无服务端 redirect）下，只有
// generateStaticParams 枚举的路径才会被构建。这里除了 6 个分组 id，还必须枚举
// 全部旧平铺 tab id（渲染 LegacySettingsRedirect 做客户端跳转）。导航可点但
// 未在此枚举的路径会 404——看起来像"功能没实现"，实际只是没被构建。
export function generateStaticParams() {
  return [
    ...SETTINGS_GROUPS.map((g) => ({ group: g.id })),
    ...Object.keys(LEGACY_REDIRECT).map((group) => ({ group })),
  ];
}

export default function SettingsGroupPage({ params }: { params: { group?: string } }) {
  const group = params.group || "";
  const legacyTo = LEGACY_REDIRECT[group];
  if (!getGroup(group) && legacyTo) {
    return <LegacySettingsRedirect to={legacyTo} />;
  }
  return <SettingsView group={group} />;
}
