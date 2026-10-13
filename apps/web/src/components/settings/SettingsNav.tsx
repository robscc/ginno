"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { SETTINGS_GROUPS } from "./settingsGroups";

// 左侧 6 行分组导航（不再分 Security/System 小节），高亮按 group id。
export function SettingsNav({ active }: { active: string }) {
  const router = useRouter();
  const t = useTranslations("settings.nav");
  return (
    <nav className="w-48 shrink-0 border-r border-line px-3 py-5">
      <div className="mb-3 px-3 text-sm font-semibold text-txt">{t("title")}</div>
      <div className="space-y-0.5">
        {SETTINGS_GROUPS.map((g) => {
          const Ic = g.icon;
          const sel = active === g.id;
          return (
            <button
              key={g.id}
              onClick={() => router.push(`/settings/${g.id}`)}
              // 菜单保持单行：缩小半档字号 + 截断（title 兜底全名），不换行
              // 挤坏导航（2026-10-14 用户定稿；英文长标签如 Models &
              // Connections 此前折成两行）。
              title={t(`groups.${g.labelKey}`)}
              className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-[12.5px] leading-tight transition-colors"
              style={{
                background: sel ? g.color + "1f" : "transparent",
                color: sel ? "#fff" : "#9a9aa6",
              }}
            >
              <Ic className="h-4 w-4 shrink-0" style={{ color: g.color }} />
              <span className="min-w-0 flex-1 truncate text-left">{t(`groups.${g.labelKey}`)}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
