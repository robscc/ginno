"use client";

import { ScheduledPage } from "@/components/scheduled/ScheduledPage";

/** 定时任务页（scheduled-tasks-design.md §3.1），入口在侧边栏 footer nav。
 *  AppShell 把非工作区路由装进一个 flex 子项，根节点要自己占满 flex 空间。 */
export default function ScheduledTasksPage() {
  return (
    <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
      <ScheduledPage />
    </div>
  );
}
