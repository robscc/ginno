"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlarmClock,
  BookOpen,
  Cable,
  Settings as SettingsIcon,
  TerminalSquare,
  Workflow as WorkflowIcon,
  Users,
} from "lucide-react";
import { UnseenBadge } from "@/components/scheduled/UnseenBadge";
import { listTasks, TASK_TERMINAL, type TaskRecord } from "@/lib/runtime";
import { notifyTaskTransitions } from "@/lib/store/notifications";

export interface SidebarFooterProps {
  onTeam: boolean;
  onConnectors: boolean;
  onScheduled: boolean;
  onTasks: boolean;
  onKb: boolean;
  onWorkflows: boolean;
  onSettings: boolean;
  /** Connector 聚合状态点："" | "warn" | "error"。 */
  connectorDot: string;
  /** 定时任务状态点："" | "off" | "error"。 */
  scheduleDot: "" | "off" | "error";
  scheduleUnseen: { total: number; error: number; missed: number };
}

/** 侧栏页脚导航（团队/连接器/定时任务/后台任务/KB/工作流/设置 + 版权行）。 */
export function SidebarFooter({
  onTeam,
  onConnectors,
  onScheduled,
  onTasks,
  onKb,
  onWorkflows,
  onSettings,
  connectorDot,
  scheduleDot,
  scheduleUnseen,
}: SidebarFooterProps) {
  const t = useTranslations("shell");
  // 后台任务全局轮询（background-tasks-design §6.2/§6.3）：SidebarFooter 进程
  // 内单实例，5s 一拉——角标与完成通知（仅后台标签页）都从这里驱动；任务
  // 中心页/聊天 TaskCard 另有自己的 2s 轮询取明细。
  const [tasksRunning, setTasksRunning] = useState(0);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const tasks: TaskRecord[] = await listTasks();
        if (!alive) return;
        notifyTaskTransitions(tasks);
        setTasksRunning(tasks.filter((x) => !TASK_TERMINAL.has(x.status)).length);
      } catch {
        /* sidecar 未起——角标保持 */
      }
    };
    void poll();
    const id = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);
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
      {/* 后台任务（background-tasks-design §6.2）：定时任务之下、KB 之上，
          运行数角标由本组件的 5s 轮询驱动。 */}
      <Link href="/tasks" className={`nav-item ${onTasks ? "nav-item-active" : ""}`}>
        <TerminalSquare className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.tasks")}</span>
        {tasksRunning > 0 && (
          <span
            className="ml-auto inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full px-1 text-[10px] font-bold text-black"
            style={{ background: "var(--st-running)" }}
            title={t("nav.tasksRunning", { n: tasksRunning })}
          >
            {tasksRunning}
          </span>
        )}
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
