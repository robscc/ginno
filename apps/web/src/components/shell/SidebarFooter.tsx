"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  AlarmClock,
  BookOpen,
  Cable,
  Settings as SettingsIcon,
  Workflow as WorkflowIcon,
  Users,
} from "lucide-react";
import { UnseenBadge } from "@/components/scheduled/UnseenBadge";

export interface SidebarFooterProps {
  onTeam: boolean;
  onConnectors: boolean;
  onScheduled: boolean;
  onKb: boolean;
  onWorkflows: boolean;
  onSettings: boolean;
  /** Connector 聚合状态点："" | "warn" | "error"。 */
  connectorDot: string;
  /** 定时任务状态点："" | "off" | "error"。 */
  scheduleDot: "" | "off" | "error";
  scheduleUnseen: { total: number; error: number; missed: number };
}

/** 侧栏页脚导航（团队/连接器/定时任务/KB/工作流/设置 + 版权行）。 */
export function SidebarFooter({
  onTeam,
  onConnectors,
  onScheduled,
  onKb,
  onWorkflows,
  onSettings,
  connectorDot,
  scheduleDot,
  scheduleUnseen,
}: SidebarFooterProps) {
  const t = useTranslations("shell");
  return (
    <div className="border-t border-line px-2.5 py-3">
      <Link href="/team" className={`nav-item ${onTeam ? "nav-item-active" : ""}`}>
        <Users className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.team")}</span>
      </Link>
      <Link href="/connectors" className={`nav-item ${onConnectors ? "nav-item-active" : ""}`}>
        <Cable className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.connectors")}</span>
        {connectorDot === "error" ? (
          <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-red-500" title={t("navDots.connectorError")} />
        ) : connectorDot === "warn" ? (
          <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-yellow-500" title={t("navDots.connectorWarn")} />
        ) : null}
      </Link>
      {/* 定时任务（scheduled-tasks-design §3.1）：Connectors 之下、KB 之上 */}
      <Link href="/scheduled" className={`nav-item ${onScheduled ? "nav-item-active" : ""}`}>
        <AlarmClock className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.scheduled")}</span>
        {scheduleDot === "error" ? (
          <UnseenBadge
            n={scheduleUnseen.total}
            tone={scheduleUnseen.error > 0 || scheduleUnseen.missed > 0 ? "red" : "violet"}
            title={t("navDots.scheduleError")}
            className="ml-auto"
          />
        ) : scheduleDot === "off" ? (
          <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-faint" title={t("navDots.scheduleOff")} />
        ) : null}
      </Link>
      <Link href="/kb" className={`nav-item ${onKb ? "nav-item-active" : ""}`}>
        <BookOpen className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.kb")}</span>
      </Link>
      <Link href="/workflows" className={`nav-item ${onWorkflows ? "nav-item-active" : ""}`}>
        <WorkflowIcon className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.workflows")}</span>
      </Link>
      <Link href="/settings/connections/model-api" className={`nav-item ${onSettings ? "nav-item-active" : ""}`}>
        <SettingsIcon className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.settings")}</span>
      </Link>

      <div className="px-2.5 pt-2 text-[10px] text-faint">© 2026 GinnoWork</div>
    </div>
  );
}
