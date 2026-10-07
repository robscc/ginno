import { SettingsView } from "@/components/settings/SettingsView";
import { SETTINGS_GROUPS } from "@/components/settings/settingsGroups";

// 静态导出下只有 generateStaticParams 枚举的组合才会被构建：枚举全部分组的
// 全部合法 sub（general 单页分组 tabs 为空，不产生子路由）。dev 模式下非法
// 组合仍会渲染，由 SettingsView 的 unknownTab 兜底；生产构建则直接 404。
export function generateStaticParams() {
  return SETTINGS_GROUPS.flatMap((g) =>
    g.tabs.map((tab) => ({ group: g.id, sub: tab.id }))
  );
}

export default function SettingsSubPage({
  params,
}: {
  params: { group?: string; sub?: string };
}) {
  return <SettingsView group={params.group || ""} sub={params.sub} />;
}
