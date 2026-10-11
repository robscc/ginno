"use client";

// QueueBar — composer 上方的 steer 排队条（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）：用户在运行中的 turn 里输入、等待下一个 superstep 吸收的
// 条目。有序、单行、可移除、可 ↑ 召回。

import { Loader2, Paperclip, X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { SteerItem } from "@/lib/steerQueue";

export function QueueBar({
  items,
  sessionId,
  onDrop,
}: {
  items: SteerItem[];
  sessionId: string;
  onDrop: (sid: string, steerId: string) => void;
}) {
  const tc = useTranslations("chat");
  return (
    <div className="mb-1 rounded-lg border border-line bg-card2/60 px-2 py-1.5">
      <div className="flex items-center justify-between px-0.5 pb-1 text-[10px] text-muted">
        <span>{tc("queue.pending", { count: items.length })}</span>
        <span className="text-faint">{tc("queue.recallHint")}</span>
      </div>
      <ol className="space-y-0.5">
        {items.map((it, i) => (
          <li
            key={it.steerId}
            className="group flex items-start gap-1.5 px-0.5 text-xs text-txt"
          >
            <span className="mt-[1px] shrink-0 text-faint">{i + 1}</span>
            <span className="min-w-0 flex-1 truncate" title={it.text}>
              {it.text || (it.images.length + it.files.length ? tc("queue.attachmentsOnly") : "")}
            </span>
            {it.images.length + it.files.length > 0 && (
              // The queued entry carries attachments too — surface the
              // count so the row is not read as text-only.
              <span
                className="mt-[1px] flex shrink-0 items-center gap-0.5 text-faint"
                title={tc("queue.attachTooltip", { images: it.images.length, files: it.files.length })}
              >
                <Paperclip className="h-3 w-3" />
                {it.images.length + it.files.length}
              </span>
            )}
            {it.status === "sending" && (
              <Loader2
                className="mt-[1px] h-3 w-3 shrink-0 animate-spin text-faint"
                aria-label={tc("queue.submitting")}
              />
            )}
            <button
              onClick={() => onDrop(sessionId, it.steerId)}
              className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-red group-hover:opacity-100"
              title={tc("queue.removeTitle")}
              aria-label={tc("queue.removeAria")}
            >
              <X className="h-3 w-3" />
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}
