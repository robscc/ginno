"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { HelpCircle, Send } from "lucide-react";

/**
 * $.ui.ask 的前端宿主(claude-code-mods-design.md §7.3):runtime 收到 mod 的
 * 提问(broker notify "mod.ask" → WS `mod.ask` 帧)后,这里渲染浮层卡片——
 * mod 名标签 + 问题正文 + choices 按钮 / 自由输入。回答经 engine 回发
 * `mod.ui.answer {id, value}`,本地乐观清卡(broker 侧 resolve 对应 Promise;
 * 孤儿 answer 由 broker 丢弃)。样式对齐 ModToastHost 的卡片语言。
 */

export interface ModAskState {
  id: string;
  mod?: string;
  message: string;
  choices: string[];
}

export function ModAskCard({
  ask,
  onAnswer,
}: {
  ask: ModAskState;
  onAnswer: (value: string) => void;
}) {
  const t = useTranslations("chat.mods");
  const [draft, setDraft] = useState("");
  const submit = (value: string) => {
    const v = value.trim();
    if (!v) return;
    onAnswer(v);
  };
  return (
    <div className="fixed bottom-20 left-1/2 z-50 w-80 -translate-x-1/2 rounded-xl border border-line bg-card2 shadow-lg">
      <div className="flex items-start gap-2 px-4 pt-3 text-xs leading-relaxed text-txt">
        <HelpCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue" />
        <div className="min-w-0 flex-1">
          {ask.mod && (
            <span className="mr-1.5 rounded bg-line/60 px-1.5 py-px font-mono text-[10px] text-muted">
              {ask.mod}
            </span>
          )}
          <span className="break-words whitespace-pre-wrap">{ask.message}</span>
        </div>
      </div>
      {ask.choices.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pt-2.5">
          {ask.choices.map((c) => (
            <button
              key={c}
              onClick={() => submit(c)}
              className="rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-txt transition-colors hover:bg-blue/10"
            >
              {c}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-center gap-1.5 px-4 py-3">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) submit(draft);
          }}
          placeholder={t("askPlaceholder")}
          className="min-w-0 flex-1 rounded-lg border border-line bg-card px-2.5 py-1.5 text-xs text-txt outline-none placeholder:text-faint focus:border-blue/50"
        />
        <button
          onClick={() => submit(draft)}
          disabled={!draft.trim()}
          className="flex shrink-0 items-center gap-1 rounded-lg border border-line bg-card px-2.5 py-1.5 text-xs text-txt transition-colors hover:bg-blue/10 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label={t("askSend")}
        >
          <Send className="h-3 w-3" />
          {t("askSend")}
        </button>
      </div>
    </div>
  );
}
