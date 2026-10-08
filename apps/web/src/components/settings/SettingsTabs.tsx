"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import type { SettingsGroup } from "./settingsGroups";

// 多 tab 分组的横向 pill 子页签，点击 router.push 到 /settings/{group}/{tabId}。
export function SettingsTabs({ group, active }: { group: SettingsGroup; active: string }) {
  const router = useRouter();
  const t = useTranslations("settings.nav");
  return (
    <div className="flex flex-wrap gap-1.5 border-b border-line px-8 py-3">
      {group.tabs.map((tab) => {
        const sel = active === tab.id;
        return (
          <button
            key={tab.id}
            onClick={() => router.push(`/settings/${group.id}/${tab.id}`)}
            className="rounded-full px-3 py-1 text-sm transition-colors"
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
