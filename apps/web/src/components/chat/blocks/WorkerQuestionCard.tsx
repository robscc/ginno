"use client";

// WorkerQuestionCard — 订阅方代答请求卡片（worker ask_user 中断透传前端层，
// 2026-11-21）。订阅方 session 收到的 <ginno_worker_question> 信封此前按裸
// 文本渲染；现在折叠成一张卡片：worker 名 + 问题 + 原选项 chips + 提示
// 「agent 稍后会以问答卡转达」。展开可看信封原文（解析兜底——永不丢信息）。
//
// 配色语义沿用聊天 chrome 约定（见 SteerBand 注释）：yellow = needs your
// attention——这正是「等你决策」语义；不复用 violet（thinking/agent）或
// orange（steer 注入）。

import { useState } from "react";
import { ChevronDown, ChevronUp, MessageCircleQuestion } from "lucide-react";
import { useChatT } from "./i18n";
import type { RelayQuestion } from "./workerQuestion";

export function WorkerQuestionCard({ items }: { items: RelayQuestion[] }) {
  const { tc } = useChatT();
  const [rawOpen, setRawOpen] = useState(false);
  return (
    <div className="my-1.5 rounded-lg border border-yellow/40 bg-yellow/[0.06] px-3 py-2">
      <div className="flex items-center gap-2">
        <MessageCircleQuestion className="h-4 w-4 shrink-0 text-yellow" aria-hidden />
        <span className="text-[13px] font-medium text-txt">
          {tc("worker.relayCard.title")}
        </span>
        {items.length === 1 ? (
          <span className="truncate rounded bg-yellow/15 px-1.5 py-px text-[11px] text-yellow">
            {items[0].title}
          </span>
        ) : (
          <span className="rounded bg-yellow/15 px-1.5 py-px text-[11px] text-yellow">
            {tc("worker.relayCard.workers", { n: items.length })}
          </span>
        )}
      </div>
      {items.map((it) => (
        <div key={it.qid || it.worker} className="mt-2 space-y-1.5">
          {it.reminder && (
            <div className="text-[11px] text-yellow/90">
              {tc("worker.relayCard.reminder")}
            </div>
          )}
          {it.subs.length > 1 && (
            <div className="text-[11px] text-muted">
              {tc("worker.relayCard.multiBadge", { n: it.subs.length })}
            </div>
          )}
          {it.subs.map((sq, i) => (
            <div key={i} className="min-w-0">
              {it.subs.length > 1 && (
                <span className="mr-1 text-[11px] text-faint">{i + 1}.</span>
              )}
              <span className="selectable text-[13px] text-txt">{sq.q}</span>
              {sq.options.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {sq.options.map((o, j) => (
                    <span
                      key={j}
                      className="selectable rounded-md border border-line2 bg-card px-1.5 py-0.5 text-[11px] text-muted"
                    >
                      {o}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
      <div className="mt-2 flex items-center gap-2 border-t border-line2/60 pt-1.5">
        <span className="min-w-0 flex-1 text-[11px] leading-snug text-muted">
          {tc("worker.relayCard.hint")}
        </span>
        <button
          onClick={() => setRawOpen((v) => !v)}
          className="flex shrink-0 items-center gap-0.5 rounded bg-card2/70 px-1.5 py-px text-[10px] text-muted hover:text-txt"
        >
          {rawOpen ? (
            <ChevronUp className="h-3 w-3" aria-hidden />
          ) : (
            <ChevronDown className="h-3 w-3" aria-hidden />
          )}
          {tc(rawOpen ? "worker.relayCard.hideRaw" : "worker.relayCard.showRaw")}
        </button>
      </div>
      {rawOpen && (
        <pre className="selectable mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-card2/50 p-2 text-[11px] text-muted">
          {items.map((it) => it.raw).join("\n\n")}
        </pre>
      )}
    </div>
  );
}
