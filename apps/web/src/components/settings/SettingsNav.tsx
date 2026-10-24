"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { SETTINGS_GROUPS } from "./settingsGroups";

// 左侧分组导航（不再分 Security/System 小节），高亮按 group id。
export function SettingsNav({ active }: { active: string }) {
  const router = useRouter();
  const t = useTranslations("settings.nav");
  // P4-2 启动自检角标：/api/health 带 startup_check 的提醒数，进设置页取一次。
  // 后端未起/字段缺失时静默不显示（自检不能反过来变成噪音）。
  const [warnings, setWarnings] = useState(0);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const h = (await api.health()) as { diagnostics_warnings?: number };
        if (alive && typeof h.diagnostics_warnings === "number") setWarnings(h.diagnostics_warnings);
      } catch {
        /* sidecar 未就绪 —— 不显示角标 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 已在目标分组时再次点击 → 滚到该分组内容（诊断页锚点 #diagnostics）。
  const go = (id: string) => {
    if (active === id) {
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    router.push(`/settings/${id}`);
  };

  return (
    <nav className="w-48 shrink-0 border-r border-line px-3 py-5">
      <div className="mb-3 flex items-center gap-2 px-3">
        <span className="min-w-0 truncate text-sm font-semibold text-txt">{t("title")}</span>
        {warnings > 0 && (
          <button
            onClick={() => go("diagnostics")}
            title={t("diagnosticsBadge", { n: warnings })}
            className="shrink-0 whitespace-nowrap rounded-full bg-yellow/15 px-1.5 py-0.5 text-[10px] text-yellow hover:bg-yellow/25"
          >
            {t("diagnosticsBadge", { n: warnings })}
          </button>
        )}
      </div>
      <div className="space-y-0.5">
        {SETTINGS_GROUPS.map((g) => {
          const Ic = g.icon;
          const sel = active === g.id;
          return (
            <button
              key={g.id}
              onClick={() => go(g.id)}
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
