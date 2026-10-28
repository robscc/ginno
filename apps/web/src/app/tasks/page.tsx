"use client";

import { TaskCenter } from "@/components/tasks/TaskCenter";

/** 后台任务中心（background-tasks-design.md §6.2），入口在侧边栏 footer nav。
 *  AppShell 把非工作区路由装进一个 flex 子项，根节点要自己占满 flex 空间。 */
export default function BackgroundTasksPage() {
  return (
    <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
      <TaskCenter />
    </div>
  );
}
