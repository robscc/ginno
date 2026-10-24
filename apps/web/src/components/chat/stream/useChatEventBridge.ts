"use client";

// useChatEventBridge — ChatStream 里那些「挂在 window 上的一次性监听」与
// composer 焦点/插入类效果（自 ChatStream.tsx 机械拆出，效果与依赖逐字一致）。

import { useEffect } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { t } from "@/i18n/provider";
import type { useChatStreamEngine } from "../useChatStreamEngine";
import { pruneMentions } from "../commandMenu";
import type { ResolvedMention } from "../commandMenu";
import type { SendPayload } from "../streamCore";
import type { ComposerInsertRequest } from "@/lib/store";

export function useChatEventBridge({
  focusLatestRef,
  stickRef,
  curSessionIdRef,
  draftCacheRef,
  mentionsRef,
  busyBySessionRef,
  textareaRef,
  session,
  input,
  setInput,
  pinToBottom,
  enqueueSteer,
  attemptSend,
  composerInsert,
  clearComposerInsert,
  recomputeMenu,
}: {
  focusLatestRef: { current: string | null };
  stickRef: { current: boolean };
  curSessionIdRef: { current: string | null };
  draftCacheRef: ReturnType<typeof useChatStreamEngine>["draftCacheRef"];
  mentionsRef: { current: Record<string, ResolvedMention[]> };
  busyBySessionRef: { current: Record<string, boolean> };
  textareaRef: MutableRefObject<HTMLTextAreaElement | null>;
  session: { id: string } | null;
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  pinToBottom: (frames?: number) => void;
  enqueueSteer: ReturnType<typeof useChatStreamEngine>["enqueueSteer"];
  attemptSend: (sid: string, payload: SendPayload, userMsgId?: string) => void;
  composerInsert: ComposerInsertRequest | null;
  clearComposerInsert: () => void;
  recomputeMenu: (text: string) => void;
}) {
  // A viewport resize reflows the transcript WITHOUT a [messages] change —
  // the 12-frame pin window has long passed, so a shorter window left the
  // latest message a few px (measured: 17) or a page above the fold with
  // stickRef still true. Re-pin on resize while stuck (2026-09-25).
  useEffect(() => {
    const onResize = () => {
      if (stickRef.current) pinToBottom();
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Notification-click jump target (dispatched by AppShell's __ginnoOpenSession
  // and by the HTML5-notification browser fallback in the message.end handler).
  // Arm stick-to-bottom; if the session is already displayed scroll now,
  // otherwise the session-switch effect handles it once it lands.
  useEffect(() => {
    const onFocusLatest = (e: Event) => {
      const sid = (e as CustomEvent<string>).detail;
      if (!sid) return;
      focusLatestRef.current = sid;
      stickRef.current = true;
      if (curSessionIdRef.current === sid) pinToBottom();
    };
    window.addEventListener("ginno:focus-latest", onFocusLatest);
    return () => window.removeEventListener("ginno:focus-latest", onFocusLatest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 子代理结果卡「去子会话纠偏」的输入框预填（P2 任务 3）。目标会话正在展示
  // 就直接进 composer；不在展示（刚点跳转、切换 effect 还没跑）就停进该会话
  // 的草稿槽——切换 effect 恢复草稿时自然带出来，文本永不丢。
  useEffect(() => {
    const onPrefill = (e: Event) => {
      const d = (e as CustomEvent<{ sessionId?: string; text?: string }>).detail;
      if (!d?.sessionId || !d.text) return;
      if (curSessionIdRef.current === d.sessionId) {
        setInput((cur) => (cur ? `${cur}\n${d.text}` : d.text!));
        requestAnimationFrame(() => textareaRef.current?.focus());
      } else {
        const prev = draftCacheRef.current[d.sessionId];
        draftCacheRef.current[d.sessionId] = {
          input: prev?.input ? `${prev.input}\n${d.text}` : d.text!,
          attachments: prev?.attachments ?? [],
          files: prev?.files ?? [],
          mentions: prev?.mentions ?? [],
        };
      }
    };
    window.addEventListener("ginno:prefill-input", onPrefill);
    return () => window.removeEventListener("ginno:prefill-input", onPrefill);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // mods 的 $.prompt.fill（fill 语义 = 接管草稿：替换而非追加）与
  // $.prompt.suggest（建议语义：composer 为空才落字，非空丢弃）。两者都
  // 只作用于正在展示的会话——后台会话没有可接管的草稿，帧自然丢弃。
  useEffect(() => {
    const onModFill = (e: Event) => {
      const d = (e as CustomEvent<{ sessionId?: string; text?: string }>).detail;
      if (!d?.sessionId || !d.text || curSessionIdRef.current !== d.sessionId) return;
      setInput(d.text);
      requestAnimationFrame(() => textareaRef.current?.focus());
    };
    const onModSuggest = (e: Event) => {
      const d = (e as CustomEvent<{ sessionId?: string; text?: string }>).detail;
      if (!d?.sessionId || !d.text || curSessionIdRef.current !== d.sessionId) return;
      // 建议语义:v1 无 ghost-text,composer 为空时替用户打好草稿,非空不打扰。
      setInput((cur) => (cur.trim() === "" ? d.text! : cur));
    };
    window.addEventListener("ginno:mod-fill-input", onModFill);
    window.addEventListener("ginno:suggest-input", onModSuggest);
    return () => {
      window.removeEventListener("ginno:mod-fill-input", onModFill);
      window.removeEventListener("ginno:suggest-input", onModSuggest);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 子代理结果卡「让主对话处理」：在当前（主）会话以一条引用该结果的消息开工。
  // 忙时走 steer 队列（既有注入管线），闲时 invoke 开新 turn——与用户手打的
  // 消息完全同一条路径。agentId 置 null = 会话默认 agent。
  useEffect(() => {
    const onEscalate = (e: Event) => {
      const d = (e as CustomEvent<{ sessionId?: string; goal?: string; summary?: string }>).detail;
      const sid = curSessionIdRef.current;
      if (!sid || !d?.sessionId) return;
      const goal = d.goal || t("subtask", "子任务");
      const brief = (d.summary || "").trim().slice(0, 300);
      const text = t(
        `A subagent task "${goal}" (session ${d.sessionId}) produced results that failed human review. Verify the subagent's conclusions and decide the next step (re-split, follow up in its session, or take over yourself). Result summary: ${brief}${brief.length >= 300 ? "…" : ""}`,
        `子代理任务「${goal}」（session ${d.sessionId}）的结果经人工检查有问题。请核对该子代理的结论并决定下一步（重新拆分、在原会话追问，或自己接手）。其结果摘要：${brief}${brief.length >= 300 ? "…" : ""}`,
      );
      if (busyBySessionRef.current[sid]) {
        enqueueSteer(sid, text, null);
      } else {
        attemptSend(sid, { text, images: [], files: [], mentions: [], agentId: null });
      }
    };
    window.addEventListener("ginno:subagent-escalate", onEscalate);
    return () => window.removeEventListener("ginno:subagent-escalate", onEscalate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Home autofocus: the composer remounts on the home slot (keyed), so focus
  // must be re-armed after the transition.
  useEffect(() => {
    if (!session) textareaRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  // 拖文件进输入框（code 工作区树 → composer）：消费 store 的一次性插入请求，
  // 在光标（textarea 未聚焦时 selectionStart 仍保留上次位置）处插入路径文本，
  // 前面非空白时补一个分隔空格，然后把焦点和光标落到插入文本之后。
  useEffect(() => {
    if (!composerInsert) return;
    clearComposerInsert();
    const el = textareaRef.current;
    const caret = el?.selectionStart ?? input.length;
    const before = input.slice(0, caret);
    const sep = before && !/\s$/.test(before) ? " " : "";
    const text = `${before}${sep}${composerInsert.text}${input.slice(caret)}`;
    const newCaret = caret + sep.length + composerInsert.text.length;
    setInput(text);
    // 与 onChange 同步逻辑对齐：插入可能打断已解析的 mention token（拖到
    // 半个 `@agent:xx` 中间），prune 掉不再出现的条目。
    if (session) {
      mentionsRef.current[session.id] = pruneMentions(
        mentionsRef.current[session.id] ?? [],
        text,
      );
    }
    requestAnimationFrame(() => {
      const el2 = textareaRef.current;
      if (el2) {
        el2.focus();
        el2.setSelectionRange(newCaret, newCaret);
      }
      recomputeMenu(text);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerInsert]);
}
