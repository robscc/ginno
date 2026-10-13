"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import type { SettingsGroup } from "./settingsGroups";

// 多 tab 分组的横向 pill 子页签，点击 router.push 到 /settings/{group}/{tabId}。
export function SettingsTabs({ group, active }: { group: SettingsGroup; active: string }) {
  const router = useRouter();
  const t = useTranslations("settings.nav");
  return (
    // 子菜单贴左（用户要求 2026-10-14）：显式 justify-start，不随父级布局漂移。
    <div className="flex flex-wrap justify-start gap-1.5 border-b border-line px-8 py-3">
      {group.tabs.map((tab) => {
        const sel = active === tab.id;
        return (
          <button
            key={tab.id}
            onClick={() => router.push(`/settings/${group.id}/${tab.id}`)}
            // 与左侧分组菜单同一单行策略：pill 不换行、截断 + title 兜底
            // （2026-10-14；英文长标签此前把 pill 撑成两行）。
            title={t(`items.${tab.label}`)}
            className="max-w-[11rem] shrink-0 truncate rounded-full px-3 py-1 text-left text-[12.5px] transition-colors"
            style={{
              background: sel ? group.color + "1f" : "transparent",
              color: sel ? "#fff" : "#9a9aa6",
            }}
          >
            {t(`items.${tab.label}`)}
          </button>
        );
      })}
    </div>
  );
}
