"use client";

// LlmCallLine — LLM 请求级可观测行（T3：llm.request / llm.response 事件对，
// 按 (reqId, attempt) 合并后的单条 llm_call block）。视觉语言完全复用既有
// 「角落 chrome」：turn_meta 的 font-mono 11px + 胶囊计量（↑输入蓝/↓输出绿）、
// toolBlocks 的琥珀 pending 脉冲。失败用琥珀而非正红——重试常能自救，
// 「显眼但不刺眼」；正红留给 turn 级失败卡。
//
// 行内渲染在 block 数组位置（时间线锚：模型调用发生在它产出的文本之前），
// 不做成大卡片；一次 turn 的多次调用 = 多行薄条目。

import { useTranslations } from "next-intl";
import { fmtTokens } from "@/lib/format";
import type { Block } from "./types";

export function LlmCallLine({ block }: { block: Extract<Block, { kind: "llm_call" }> }) {
  const tc = useTranslations("chat");
  const failed = block.ok === false;
  // 前端哨兵（closePendingBlocks 定格）：turn 中止 ≠ 模型失败，中性灰。
  const interrupted = failed && block.error === "interrupted";
  const secs = typeof block.elapsedMs === "number" ? block.elapsedMs / 1000 : null;
  const elapsedLabel =
    secs == null ? null : secs >= 10 ? `${Math.round(secs)}s` : `${secs.toFixed(1)}s`;
  // 元数据提取失败（后端退化 {}）时模型名留空——回退 locale 无关的 "LLM"。
  const model = block.model || "LLM";
  const tips: string[] = [];
  if (block.provider) tips.push(block.provider);
  if (typeof block.messagesCount === "number") {
    tips.push(tc("llm-call-messages", { n: block.messagesCount }));
  }
  if (typeof block.inputTokens === "number") {
    tips.push(`${tc("turnMeta.detailIn")} ${block.inputTokens.toLocaleString()}`);
  }
  if (typeof block.outputTokens === "number") {
    tips.push(`${tc("turnMeta.detailOut")} ${block.outputTokens.toLocaleString()}`);
  }
  if (typeof block.cacheReadTokens === "number" && block.cacheReadTokens > 0) {
    tips.push(`cache ${fmtTokens(block.cacheReadTokens)}`);
  }
  const chip = "rounded-md bg-card2/60 px-1.5 py-px";
  return (
    <div
      data-testid="llm-call-line"
      title={tips.length ? tips.join(" · ") : undefined}
      className="flex select-none flex-wrap items-center gap-x-1.5 gap-y-0.5 py-0.5 font-mono text-[11px] leading-4 text-muted/80"
    >
      <span
        className={`flex items-center gap-1 ${
          failed && !interrupted ? "text-amber-600 dark:text-amber-400" : ""
        }`}
      >
        <span aria-hidden>{failed && !interrupted ? "✖" : "⚡"}</span>
        <span className="max-w-[200px] truncate">{model}</span>
      </span>
      {block.attempt > 1 && (
        <span
          className="shrink-0 rounded border border-amber-500/40 px-1 text-[9px] leading-4 font-medium text-amber-600 dark:text-amber-400"
          title={tc("llm-call-attempt", { n: block.attempt })}
        >
          ↻{block.attempt}
        </span>
      )}
      {block.pending ? (
        <span className="animate-pulse text-amber-600/90 dark:text-amber-400/90">
          {tc("llm-call-pending")}
        </span>
      ) : (
        <>
          {elapsedLabel != null && <span className={chip}>{elapsedLabel}</span>}
          {typeof block.inputTokens === "number" && (
            <span className={`${chip} text-sky-600 dark:text-sky-400`}>
              <span className="font-bold">↑</span>
              {fmtTokens(block.inputTokens)}
            </span>
          )}
          {typeof block.outputTokens === "number" && (
            <span className={`${chip} text-emerald-600 dark:text-emerald-400`}>
              <span className="font-bold">↓</span>
              {fmtTokens(block.outputTokens)}
            </span>
          )}
          {failed && !interrupted && (
            <span className="text-amber-600 dark:text-amber-400">
              {tc("llm-call-failed")}
              {block.error ? ` · ${block.error}` : ""}
            </span>
          )}
          {interrupted && <span className="text-faint">{tc("llm-call-interrupted")}</span>}
        </>
      )}
    </div>
  );
}
