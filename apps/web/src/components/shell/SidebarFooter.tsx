"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlarmClock,
  Sparkles,
  BookOpen,
  Cable,
  Settings as SettingsIcon,
  TerminalSquare,
  Workflow as WorkflowIcon,
  Users,
} from "lucide-react";
import { UnseenBadge } from "@/components/scheduled/UnseenBadge";
import { NoticeBell } from "@/components/notice/NoticeBell";
import { BASE, listTasks, TASK_TERMINAL, type TaskRecord } from "@/lib/runtime";
import { notifyTaskTransitions } from "@/lib/store/notifications";

export interface SidebarFooterProps {
  onTeam: boolean;
  onConnectors: boolean;
  onScheduled: boolean;
  onTasks: boolean;
  onKb: boolean;
  onInstincts: boolean;
  onWorkflows: boolean;
  onSettings: boolean;
  /** Connector 聚合状态点："" | "warn" | "error"。 */
  connectorDot: string;
  /** 定时任务状态点："" | "off" | "error"。 */
  scheduleDot: "" | "off" | "error";
  /** 执行中 run 数（2026-11-19）：>0 时红徽标左侧显示迷你彩虹点 */
  scheduleRunning?: number;
  scheduleUnseen: { total: number; error: number; missed: number };
}

/** 侧栏页脚导航（团队/连接器/定时任务/后台任务/KB/工作流/设置 + 版权行）。 */
export function SidebarFooter({
  onTeam,
  onConnectors,
  onScheduled,
  onTasks,
  onKb,
  onInstincts,
  onWorkflows,
  onSettings,
  connectorDot,
  scheduleDot,
  scheduleRunning,
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
  // AgentTeam 全局徽标（backlog P1-17，设计 §5.3.1 三级上卷的导航层）：
  // GET /api/teams/badge 直读持久化态（零刷新，廉价），与 Dock 的 badge.rs
  // 同口径——只有 blocked / done 未读冒泡，working 止于团队行。轮询节奏
  // 照上方后台任务的 5s 先例；失败保持旧值（sidecar 未起不闪徽标）。
  const [teamBadge, setTeamBadge] = useState<{
    needs: number;
    blocked: number;
    done_unread: number;
  } | null>(null);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const r = await fetch(`${BASE}/teams/badge`);
        const d = (await r.json()) as {
          ok?: boolean;
          needs?: number;
          blocked?: number;
          done_unread?: number;
        };
        if (alive && d.ok) {
          // 空闲治理（2026-11-15）：5s 轮询数值不变保引用 bailout，避免
          // 每 5s 新对象触发重渲染。
          const needs = d.needs ?? 0;
          const blocked = d.blocked ?? 0;
          const done_unread = d.done_unread ?? 0;
          setTeamBadge((prev) =>
            prev &&
            prev.needs === needs &&
            prev.blocked === blocked &&
            prev.done_unread === done_unread
              ? prev
              : { needs, blocked, done_unread },
          );
        }
      } catch {
        /* sidecar 未起——徽标保持 */
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
        {/* needs_action 徽标（backlog P1-17）：blocked+done 未读合计，与
            团队行徽标同视觉语言——有 blocked 用黄（等你回答），纯 done 未读
            用紫（好消息不飘红，UnseenBadge 同款纪律）。0 不渲染。 */}
        {teamBadge && teamBadge.needs > 0 && (
          <span
            className={`ml-auto shrink-0 rounded-full border px-1.5 text-[10px] leading-4 tabular-nums ${
              teamBadge.blocked > 0
                ? "border-yellow-500/40 bg-yellow-500/10 text-yellow-500"
                : "border-violet/40 bg-violet/10 text-violet"
            }`}
            title={t("navDots.teamNeeds", { n: teamBadge.needs })}
          >
            {teamBadge.needs > 99 ? "99+" : teamBadge.needs}
          </span>
        )}
        {/* Beta 标（2026-11-05）：AgentTeam 功能未达稳定，入口明示。 */}
        <span className={`${teamBadge && teamBadge.needs > 0 ? "" : "ml-auto"} shrink-0 rounded border border-amber-500/50 bg-amber-500/10 px-1 py-px text-[9px] font-medium uppercase leading-none tracking-wide text-amber-600 dark:text-amber-400`}>
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
        {(scheduleRunning ?? 0) > 0 && (
          <span className="ml-auto inline-flex shrink-0 items-center gap-1" title={t("navDots.scheduleRunning")}>
            <span className="rainbow-mini-dot" aria-hidden />
            <span className="text-[11px] font-bold" style={{ color: "var(--st-running)" }}>
              {scheduleRunning}
            </span>
          </span>
        )}
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
            className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] font-bold"
            style={{ color: "var(--st-running)" }}
            title={t("nav.tasksRunning", { n: tasksRunning })}
          ><span className="rainbow-mini-dot" aria-hidden />
            {tasksRunning}
          </span>
        )}
      </Link>
      <Link href="/kb" className={`nav-item ${onKb ? "nav-item-active" : ""}`}>
        <BookOpen className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.kb")}</span>
      </Link>
      {/* 本能（instincts，docs/instincts-design.md）：行为规则沉淀层。
          BETA 标（2026-11-10 用户点名）：P0 仅确定性捕获+模板规则。 */}
      <Link href="/instincts" className={`nav-item ${onInstincts ? "nav-item-active" : ""}`}>
        <Sparkles className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.instincts")}</span>
        <span className="ml-auto shrink-0 rounded border border-amber-500/50 bg-amber-500/10 px-1 py-px text-[9px] font-medium uppercase leading-none tracking-wide text-amber-600 dark:text-amber-400">
          Beta
        </span>
      </Link>
      <Link href="/workflows" className={`nav-item ${onWorkflows ? "nav-item-active" : ""}`}>
        <WorkflowIcon className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.workflows")}</span>
      </Link>
      <Link href="/settings/connections/model-api" className={`nav-item ${onSettings ? "nav-item-active" : ""}`}>
        <SettingsIcon className="h-4 w-4 shrink-0" />
        <span className="truncate">{t("nav.settings")}</span>
      </Link>

      {/* 通知中心（2026-11-21 从 session TopBar 迁入——全局功能住应用级
          位置；原 TopBar 入口按用户要求移除）。 */}
      <div className="flex items-center justify-between px-2.5 pt-2">
        <span className="text-[10px] text-faint">© 2026 GinnoWork</span>
        <NoticeBell />
      </div>

    </div>
  );
}
