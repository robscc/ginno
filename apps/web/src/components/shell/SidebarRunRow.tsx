"use client";

import { useTranslations } from "next-intl";
import { Square, Trash2, Workflow as WorkflowIcon } from "lucide-react";
import { RUN_STATUS_META, useRunStatusLabel } from "@/components/chat/RunBlocks";
import { relTime } from "@/lib/utils";
import type { WorkflowRun } from "@/lib/types";

export interface SidebarRunRowProps {
  run: WorkflowRun;
  depth: number;
  /** 当前正查看的运行（onWorkspace && activeRunId === run.id）。 */
  selected: boolean;
  /** workflow 定义名（run.name 缺省时的回退标题）。 */
  workflowName?: string;
  onOpen: () => void;
  onCancel: () => void;
  onDelete: () => void;
}

/**
 * 侧栏 workflow 运行伪条目：glyph/标签来自 RUN_STATUS_META，标题优先
 * run.name、缺省退回 workflow 定义名；点击进入中央 run 视图（与选中会话行
 * 同款高亮）。hover 入口对齐子会话行：运行中/暂停 → 取消，结束态 → 删除
 * 运行记录。事件语义由调用方注入，这里只负责渲染。
 */
export function SidebarRunRow({
  run: r,
  depth,
  selected: sel,
  workflowName,
  onOpen,
  onCancel,
  onDelete,
}: SidebarRunRowProps) {
  const t = useTranslations("shell");
  const runStatusLabel = useRunStatusLabel();
  const meta = RUN_STATUS_META[r.status];
  const active = r.status === "running" || r.status === "paused";
  return (
    <div
      className={`nav-item group ${sel ? "text-txt" : ""} ${active ? "" : "opacity-60"}`}
      style={{
        ...(sel ? { background: "rgba(99,102,241,0.14)" } : undefined),
        ...(depth > 0 ? { paddingLeft: `${10 + depth * 16}px` } : undefined),
      }}
    >
      <button
        onClick={onOpen}
        className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
      >
        {meta ? (
          <span
            className="shrink-0 text-[11px] leading-none"
            title={t("session.statusPrefix", { status: runStatusLabel(r.status) })}
          >
            {meta.emoji}
          </span>
        ) : (
          <WorkflowIcon className="h-4 w-4 shrink-0 text-muted" />
        )}
        <span className="truncate">{r.name || workflowName || t("run.fallbackName")}</span>
        <span className="ml-auto shrink-0 text-[10px] text-faint">{relTime(r.started)}</span>
      </button>
      <span className="flex shrink-0 items-center gap-0.5">
        {active ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onCancel();
            }}
            aria-label={t("run.cancel")}
            title={t("run.cancelHint")}
            className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-yellow group-hover:opacity-100"
          >
            <Square className="h-3 w-3" />
          </button>
        ) : (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            aria-label={t("run.delete")}
            title={t("run.deleteHint")}
            className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-red group-hover:opacity-100"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </span>
    </div>
  );
}
