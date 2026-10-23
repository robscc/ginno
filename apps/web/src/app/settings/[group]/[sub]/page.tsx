import { SettingsView } from "@/components/settings/SettingsView";
import { LegacySettingsRedirect } from "@/components/settings/LegacySettingsRedirect";
import { LEGACY_SUB_REDIRECT, SETTINGS_GROUPS } from "@/components/settings/settingsGroups";

// 静态导出下只有 generateStaticParams 枚举的组合才会被构建：枚举全部分组的
// 全部合法 sub（general 单页分组 tabs 为空，不产生子路由），外加已删除页签
// 的旧 URL（渲染 LegacySettingsRedirect）。dev 模式下非法组合仍会渲染，
// 由 SettingsView 的 unknownTab 兜底；生产构建则直接 404。
export function generateStaticParams() {
  return [
    ...SETTINGS_GROUPS.flatMap((g) =>
      g.tabs.map((tab) => ({ group: g.id, sub: tab.id }))
    ),
    ...Object.keys(LEGACY_SUB_REDIRECT).map((path) => {
      const [group, sub] = path.split("/");
      return { group, sub };
    }),
  ];
}

export default function SettingsSubPage({
  params,
}: {
  params: { group?: string; sub?: string };
}) {
  const group = params.group || "";
  const sub = params.sub || "";
  const legacyTo = LEGACY_SUB_REDIRECT[`${group}/${sub}`];
  if (legacyTo) {
    return <LegacySettingsRedirect to={legacyTo} />;
  }
  return <SettingsView group={group} sub={sub} />;
}
