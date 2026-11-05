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
import { Flame } from "lucide-react";
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
  const [devSwapRepo, setDevSwapRepo] = useState<string | null>(null);
  const [swapping, setSwapping] = useState(false);
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const s = (await (await import("@/lib/runtime")).getSettings()) as {
          context?: { dev_swap_repo?: string };
        };
        if (alive) setDevSwapRepo(s.context?.dev_swap_repo || null);
      } catch {
        /* sidecar 未起——按钮不显示 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
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
        {/* Beta 标（2026-11-05）：AgentTeam 功能未达稳定，入口明示。 */}
        <span className="ml-auto shrink-0 rounded border border-amber-500/50 bg-amber-500/10 px-1 py-px text-[9px] font-medium uppercase leading-none tracking-wide text-amber-600 dark:text-amber-400">
          Beta
        </span>
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

      {/* 热更新（2026-11-05）：settings context.dev_swap_repo 配置后才显示。
          点击 POST /api/devswap/start（无会话发起）→ 进度走 TopBar 徽标 +
          hover 卡片，构建完成后全局广播确认门。 */}
      {devSwapRepo && (
        <button
          type="button"
          disabled={swapping}
          onClick={async () => {
            setSwapping(true);
            try {
              const r = await fetch("/api/devswap/start", {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({}),
              });
              if (!r.ok) console.warn("[dev-swap] start failed:", r.status, await r.text());
            } catch (e) {
              console.warn("[dev-swap] start unavailable:", e);
            } finally {
              // 立即恢复可点：真正的进行中状态由 TopBar 进度徽标表达，
              // 重复点击有后端 409 already_running 兜底。
              setSwapping(false);
            }
          }}
          className="mx-2.5 mb-1.5 flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/[0.08] px-2 py-1 text-[11px] text-amber-600 hover:bg-amber-500/15 disabled:opacity-50 dark:text-amber-400"
          title={devSwapRepo}
        >
          <Flame className="h-3.5 w-3.5" />
          {t("nav.hotSwap")}
        </button>
      )}
    </div>
  );
}
