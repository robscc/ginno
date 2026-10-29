"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Check, CornerDownLeft, HelpCircle, SkipForward } from "lucide-react";
import { Markdown } from "@/components/chat/Markdown";
import type { QuestionBlock, QuestionSub } from "./blocks";

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

/** Option entry → display label.
 *
 * Backend `normalize_options` already unwraps rich option objects
 * (`{label, description, key}`) — but history replay of older transcripts, and
 * any future shape drift, can still hand the card an object. React renders an
 * object child as its JSON dump (observed 2026-10-29: buttons showing the whole
 * `{'description': …, 'label': …}` literal), so defend here too. */
function optionLabel(o: unknown): string {
  if (typeof o === "string") return o;
  if (o && typeof o === "object") {
    const rec = o as Record<string, unknown>;
    for (const k of ["label", "text", "title", "name", "value", "option", "description"]) {
      const v = rec[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    try {
      return JSON.stringify(o);
    } catch {
      return String(o);
    }
  }
  return String(o ?? "");
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
  // 多问题卡的逐题草稿：选项点选（非立即发送）与自由文本，提交时一次发完。
  const [drafts, setDrafts] = useState<Record<number, { optionIndex: number | null; text: string }>>({});
  // composer 域 catalog（必须在早退 return 之前取 hook，保证 hooks 顺序稳定）
  const t = useTranslations("composer");

  const multiSubs = block.multi && block.questions && block.questions.length >= 2 ? block.questions : null;

  // Answered/skipped collapse to a one-line receipt (same shape as
  // HumanInputCard's done state) — the card must not dominate the transcript
  // after the choice was made.
  if (block.status === "answered") {
    if (multiSubs) {
      return (
        <div className="mt-2 rounded-md border border-line bg-card2/40 px-2.5 py-1.5 text-[11px] text-muted">
          <div className="mb-1 flex items-center gap-1.5 font-medium">
            <Check className="h-3 w-3 shrink-0 text-green" /> {t("askUser.answeredN", { count: multiSubs.length })}
          </div>
          <div className="flex flex-col gap-0.5">
            {multiSubs.map((sq, i) => (
              <div key={i} className="flex items-start gap-1.5">
                <span className="shrink-0 text-faint">{i + 1}.</span>
                <span className="min-w-0 break-words">
                  {sq.skipped || !sq.answer ? (
                    <span className="text-faint">{t("askUser.skippedItem")}</span>
                  ) : (
                    <>
                      <span className="text-txt">{sq.answer}</span>
                      {sq.header ? <span className="text-faint"> — {sq.header}</span> : null}
                    </>
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      );
    }
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-line bg-card2/40 px-2.5 py-1.5 text-[11px] text-muted">
        <Check className="h-3 w-3 shrink-0 text-green" /> {t("askUser.chosen", { answer: block.answer ?? "" })}
      </div>
    );
  }
  if (block.status === "skipped") {
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-line bg-card2/40 px-2.5 py-1.5 text-[11px] text-muted">
        <SkipForward className="h-3 w-3 shrink-0" /> {t("askUser.skipped")}
      </div>
    );
  }

  const interactive = !!live && !!onAnswer;
  const send = (answer: string, optionIndex: number | null, skip: boolean) => {
    if (interactive) onAnswer!(block.id ?? "", answer, optionIndex, skip);
  };

  // ---- 多问题卡：每题独立选/填，一次提交（answers 序列化进 answer 字段，
  // actions 侧拆回数组走 multi 恢复通道）。----
  if (multiSubs) {
    const draftOf = (i: number) => drafts[i] ?? { optionIndex: null, text: "" };
    const setDraft = (i: number, patch: Partial<{ optionIndex: number | null; text: string }>) =>
      setDrafts((prev) => ({ ...prev, [i]: { ...draftOf(i), ...patch } }));
    const allAnswered = multiSubs.every((_, i) => {
      const d = draftOf(i);
      return d.optionIndex !== null || d.text.trim() !== "";
    });
    const submitAll = () => {
      if (!interactive || !allAnswered) return;
      const answers = multiSubs.map((_, i) => {
        const d = draftOf(i);
        return d.optionIndex !== null
          ? { answer: d.text.trim(), option_index: d.optionIndex, skip: false }
          : { answer: d.text.trim(), option_index: null, skip: false };
      });
      // 复用单答通道：answer 载 JSON 数组（actions.answerQuestionMulti 解包）
      send(JSON.stringify(answers), null, false);
    };
    const skipAll = () => send("", null, true);
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
          {block.header || t("askUser.defaultHeader")}
          <span className="text-faint">· {multiSubs.length}</span>
        </div>
        {block.question.trim() && (
          <div className="mb-2 text-xs leading-relaxed text-txt [&_p]:my-1">
            <Markdown text={block.question} />
          </div>
        )}
        <div className="flex flex-col gap-2.5">
          {multiSubs.map((sq, i) => (
            <SubQuestionFields
              key={i}
              idx={i}
              sub={sq}
              draft={draftOf(i)}
              interactive={interactive}
              onPick={(oi) => setDraft(i, { optionIndex: oi })}
              onText={(text) => setDraft(i, { optionIndex: null, text })}
            />
          ))}
        </div>
        <div className="mt-2.5 flex items-center gap-2.5">
          <button
            onClick={submitAll}
            disabled={!interactive || !allAnswered}
            className="btn-press rounded-md border border-violet/60 bg-violet/10 px-3 py-1.5 text-xs font-medium text-txt transition-colors hover:bg-violet/20 disabled:opacity-40"
          >
            {t("askUser.submitAll")}
          </button>
          <button
            onClick={skipAll}
            disabled={!interactive}
            className="btn-press text-[11px] text-faint underline-offset-2 hover:text-muted hover:underline disabled:opacity-50"
          >
            {t("askUser.skip")}
          </button>
        </div>
      </div>
    );
  }

  const sendFreeText = () => {
    // 局部变量避开 t：翻译函数 t 在本组件作用域（遮蔽 t 是 i18n 已知坑）
    const text = freeText.trim();
    if (text) send(text, null, false);
  };
  // Real options win. Only a question that carried NONE gets buttons parsed out
  // of its body — those reply as free text (index null), since the model never
  // assigned them an option_index.
  const real = block.options ?? [];
  const choices: { label: string; index: number | null }[] =
    real.length > 0
      ? real.map((label, i) => ({ label: optionLabel(label), index: i }))
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
        {block.header || t("askUser.defaultHeader")}
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
            placeholder={t("askUser.freeTextPlaceholder")}
            className="w-full rounded border border-line bg-card px-2 py-1.5 text-xs text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none disabled:opacity-50"
          />
          <button
            onClick={sendFreeText}
            disabled={!interactive || !freeText.trim()}
            title={t("askUser.sendCustom")}
            aria-label={t("askUser.sendCustom")}
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
        {t("askUser.skip")}
      </button>
    </div>
  );
}

/** 多问题卡的单个子题：选项按钮组（点选不发送）+ 自由文本二选一作答。 */
function SubQuestionFields({
  idx,
  sub,
  draft,
  interactive,
  onPick,
  onText,
}: {
  idx: number;
  sub: QuestionSub;
  draft: { optionIndex: number | null; text: string };
  interactive: boolean;
  onPick: (optionIndex: number) => void;
  onText: (text: string) => void;
}) {
  const t = useTranslations("composer");
  const choices: { label: string; index: number | null }[] =
    sub.options.length > 0
      ? sub.options.map((label, i) => ({ label: optionLabel(label), index: i }))
      : parseInlineOptions(sub.question).map((label) => ({ label, index: null }));
  return (
    <div className="rounded-md border border-line bg-card/60 p-2">
      <div className="mb-1.5 flex items-baseline gap-1.5 text-xs">
        <span className="shrink-0 font-semibold text-violet">{idx + 1}.</span>
        {sub.header && <span className="shrink-0 font-medium text-txt">{sub.header}</span>}
      </div>
      <div className="mb-1.5 text-xs leading-relaxed text-txt [&_p]:my-1">
        <Markdown text={sub.question} />
      </div>
      {choices.length > 0 && (
        <div className="mb-1.5 flex flex-col gap-1">
          {choices.map((c, i) => (
            <button
              key={i}
              onClick={() => onPick(c.index ?? i)}
              disabled={!interactive}
              className={`btn-press w-full rounded-md border px-2.5 py-1.5 text-left text-xs transition-colors disabled:opacity-50 ${
                draft.optionIndex === (c.index ?? i)
                  ? "border-violet bg-violet/15 text-txt"
                  : "border-line bg-card text-txt hover:border-violet/60 hover:bg-violet/10"
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
      {sub.allowFreeText && (
        <input
          value={draft.optionIndex !== null ? "" : draft.text}
          onChange={(e) => onText(e.target.value)}
          disabled={!interactive || draft.optionIndex !== null}
          placeholder={t("askUser.freeTextPlaceholder")}
          className="w-full rounded border border-line bg-card px-2 py-1.5 text-xs text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none disabled:opacity-50"
        />
      )}
    </div>
  );
}
