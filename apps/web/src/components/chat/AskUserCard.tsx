"use client";

import { useState } from "react";
import { Check, CornerDownLeft, HelpCircle, SkipForward } from "lucide-react";
import { Markdown } from "@/components/chat/Markdown";
import type { QuestionBlock } from "./blocks";

/**
 * Numbered/bulleted lines in a question BODY → quick-reply labels.
 *
 * Models sometimes inline their choices in the question text ("1. 装这里
 * 2. 装那里") instead of passing `options` — observed live 2026-09-20 (turn
 * aa49c475), where the model fell back to prose after its array argument was
 * rejected twice. Without this the user has to TYPE a number to answer a
 * question the model already laid out as a list.
 *
 * Purely presentational, and deliberately conservative: only used when the
 * call carried no real `options`, needs 2-6 matches, and the reply sends the
 * matched line as-is (free text — the intended `option_index` is unknowable,
 * and a guess would name the wrong label in the receipt).
 */
export function parseInlineOptions(question: string): string[] {
  const out: string[] = [];
  for (const raw of (question || "").split("\n")) {
    const m = /^(?:\*\*)?(?:[-*•]|\(?\d{1,2}[.)、）]|（\d{1,2}）)\s*(.+)$/.exec(raw.trim());
    if (!m) continue;
    const label = m[1].replace(/\*\*/g, "").trim();
    if (label) out.push(label);
  }
  return out.length >= 2 && out.length <= 6 ? out : [];
}

/**
 * In-chat ask_user card — the agent hit an ambiguity and parked the turn on a
 * LangGraph interrupt. Lives inside the assistant bubble (sibling of
 * workflow/HumanInputCard: same "card in a bubble, answered over a resume
 * channel" pattern); the answer rides ChatStream's `user_answer` WS message
 * and the authoritative receipt folds back via the tool's tool.end.
 *
 * `live` is false when the turn is no longer running — a still-pending card
 * then renders disabled, since its answer could never reach the graph
 * (defensive; the stop path normally flips the block to "skipped" first).
 */
export function AskUserCard({
  block,
  live,
  onAnswer,
}: {
  block: QuestionBlock;
  live?: boolean;
  onAnswer?: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
}) {
  const [freeText, setFreeText] = useState("");

  // Answered/skipped collapse to a one-line receipt (same shape as
  // HumanInputCard's done state) — the card must not dominate the transcript
  // after the choice was made.
  if (block.status === "answered") {
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-line bg-card2/40 px-2.5 py-1.5 text-[11px] text-muted">
        <Check className="h-3 w-3 shrink-0 text-green" /> 已选择：{block.answer}
      </div>
    );
  }
  if (block.status === "skipped") {
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-line bg-card2/40 px-2.5 py-1.5 text-[11px] text-muted">
        <SkipForward className="h-3 w-3 shrink-0" /> 已跳过 · 按 agent 判断继续
      </div>
    );
  }

  const interactive = !!live && !!onAnswer;
  const send = (answer: string, optionIndex: number | null, skip: boolean) => {
    if (interactive) onAnswer!(block.id ?? "", answer, optionIndex, skip);
  };
  const sendFreeText = () => {
    const t = freeText.trim();
    if (t) send(t, null, false);
  };
  // Real options win. Only a question that carried NONE gets buttons parsed out
  // of its body — those reply as free text (index null), since the model never
  // assigned them an option_index.
  const real = block.options ?? [];
  const choices: { label: string; index: number | null }[] =
    real.length > 0
      ? real.map((label, i) => ({ label, index: i }))
      : parseInlineOptions(block.question).map((label) => ({ label, index: null }));

  return (
    <div
      className={`mt-2 rounded-md border-2 p-3 ${
        interactive ? "border-yellow/40 bg-yellow/[0.05]" : "border-line bg-card2/30"
      }`}
    >
      <div
        className={`mb-1.5 flex items-center gap-1.5 text-xs font-medium ${
          interactive ? "text-yellow" : "text-muted"
        }`}
      >
        <HelpCircle className="h-3.5 w-3.5" />
        {block.header || "需要你的选择"}
      </div>
      <div className="mb-2 text-xs leading-relaxed text-txt [&_p]:my-1">
        <Markdown text={block.question} />
      </div>
      {choices.length > 0 && (
        <div className="mb-2 flex flex-col gap-1.5">
          {choices.map((c, i) => (
            <button
              key={i}
              onClick={() => send(c.label, c.index, false)}
              disabled={!interactive}
              className="btn-press w-full rounded-md border border-line bg-card px-2.5 py-1.5 text-left text-xs text-txt transition-colors hover:border-violet/60 hover:bg-violet/10 disabled:opacity-50 disabled:hover:border-line disabled:hover:bg-card"
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
      {block.allowFreeText && (
        <div className="mb-2 flex items-center gap-1.5">
          <input
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            onKeyDown={(e) => {
              // IME guard (same as the composer): Enter commits a CJK
              // candidate first and must not send mid-composition.
              if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                e.preventDefault();
                sendFreeText();
              }
            }}
            disabled={!interactive}
            placeholder="其他（输入后回车发送）"
            className="w-full rounded border border-line bg-card px-2 py-1.5 text-xs text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none disabled:opacity-50"
          />
          <button
            onClick={sendFreeText}
            disabled={!interactive || !freeText.trim()}
            title="发送自定义回答"
            aria-label="发送自定义回答"
            className="btn-press flex h-[30px] w-8 shrink-0 items-center justify-center rounded-md border border-line bg-card text-muted hover:border-violet/60 hover:text-txt disabled:opacity-40"
          >
            <CornerDownLeft className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      <button
        onClick={() => send("", null, true)}
        disabled={!interactive}
        className="btn-press text-[11px] text-faint underline-offset-2 hover:text-muted hover:underline disabled:opacity-50 disabled:hover:no-underline"
      >
        跳过 · 按你的判断继续
      </button>
    </div>
  );
}
