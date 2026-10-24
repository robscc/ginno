"use client";

// useComposerSend — composer 的发送管线（自 ChatStream.tsx 机械拆出，行为
// 逐字一致）：↑/↓ 输入历史、parked 卡片的「回答/改主意」分派、忙碌时的
// steering 排队、首页懒建会话后的首发。attemptSend 仍留在 ChatStream（它与
// engine 的 refs 互相依赖），这里只调用它。

import { useRef, type Dispatch, type SetStateAction } from "react";
import { useTranslations } from "next-intl";
import type { useGinno } from "@/lib/store";
import type { SessionMeta } from "@/lib/types";
import type { QuestionBlock } from "@/components/chat/blocks";
import type { useChatStreamEngine } from "../useChatStreamEngine";
import type { useModelPick } from "../useModelPick";
import type { useComposerFiles } from "../useComposerFiles";
import { pruneMentions, dedupeMentions, type MenuItem, type ResolvedMention, type Trigger } from "../commandMenu";
import {
  appendEntry,
  initHistory,
  seedEntries,
  type HistoryState,
} from "../inputHistory";
import {
  type Attachment,
  type ChatMsg,
  type FileAttachment,
  type PermissionPrompt,
  type SendPayload,
  type VersionPropose,
} from "../streamCore";

export function useComposerSend({
  g,
  session,
  input,
  setInput,
  attachments,
  setAttachments,
  fileAttachments,
  setFileAttachments,
  target,
  setTarget,
  setMenu,
  parked,
  permission,
  propose,
  imagesReading,
  showComposerHint,
  attemptSend,
  storeRef,
  busyBySessionRef,
  mentionsRef,
  enqueueSteer,
  respond,
  respondPropose,
  answerQuestion,
  homeCreatingRef,
  homeModel,
  pendingMounts,
  resetPendingMounts,
  curSessionIdRef,
  connectSession,
  pendingDocsRef,
  pendingPathsRef,
  uploadOneDoc,
  attachOne,
  waitForSocketOpen,
}: {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  attachments: Attachment[];
  setAttachments: Dispatch<SetStateAction<Attachment[]>>;
  fileAttachments: FileAttachment[];
  setFileAttachments: Dispatch<SetStateAction<FileAttachment[]>>;
  target: string | null;
  setTarget: Dispatch<SetStateAction<string | null>>;
  setMenu: Dispatch<SetStateAction<{ items: MenuItem[]; active: number; trigger: Trigger } | null>>;
  parked: boolean;
  permission: PermissionPrompt | null;
  propose: VersionPropose | null;
  imagesReading: number;
  showComposerHint: (msg: string) => void;
  attemptSend: (sid: string, payload: SendPayload, userMsgId?: string) => void;
  storeRef: { current: Record<string, ChatMsg[]> };
  busyBySessionRef: { current: Record<string, boolean> };
  mentionsRef: { current: Record<string, ResolvedMention[]> };
  enqueueSteer: ReturnType<typeof useChatStreamEngine>["enqueueSteer"];
  respond: ReturnType<typeof useChatStreamEngine>["respond"];
  respondPropose: ReturnType<typeof useChatStreamEngine>["respondPropose"];
  answerQuestion: ReturnType<typeof useChatStreamEngine>["answerQuestion"];
  homeCreatingRef: { current: boolean };
  homeModel: ReturnType<typeof useModelPick>["homeModel"];
  pendingMounts: ReturnType<typeof useModelPick>["pendingMounts"];
  resetPendingMounts: ReturnType<typeof useModelPick>["resetPendingMounts"];
  curSessionIdRef: { current: string | null };
  connectSession: ReturnType<typeof useChatStreamEngine>["connectSession"];
  pendingDocsRef: { current: Array<{ file: File; tmpId: string }> };
  pendingPathsRef: { current: Array<{ path: string; tmpId: string }> };
  uploadOneDoc: ReturnType<typeof useComposerFiles>["uploadOneDoc"];
  attachOne: ReturnType<typeof useComposerFiles>["attachOne"];
  waitForSocketOpen: ReturnType<typeof useChatStreamEngine>["waitForSocketOpen"];
}) {
  const tc = useTranslations("chat");
  // Composer ↑/↓ input history, per session (shell-style recall; the seed
  // comes from the session's already-loaded user messages, so a reload or
  // device switch still recalls the full history). See inputHistory.ts.
  const inputHistoryRef = useRef<Record<string, HistoryState>>({});

  /** The pending ask_user card of the displayed session, if any. */
  function pendingQuestionBlock(): QuestionBlock | null {
    for (const m of storeRef.current[curSessionIdRef.current ?? ""] ?? []) {
      for (const b of m.blocks) {
        if (b.kind === "question" && b.status === "pending") return b;
      }
    }
    return null;
  }

  /** Typing at a parked card means "answer" or "change of mind" (design §3.4):
   *  the turn is waiting on the user, not working, so this never queues. */
  function handleParkedSend(sid: string, text: string, agentId: string | null) {
    if (permission) {
      // Change of mind: don't run this tool, do what I just said instead. The
      // steer is stashed FIRST (one socket is FIFO), so the resumed segment
      // absorbs it — the deny routes straight back to `agent` (graph.py), which
      // is why the parked case needs no second injection mechanism.
      enqueueSteer(sid, text, agentId);
      respond("deny");
      return;
    }
    if (propose) {
      enqueueSteer(sid, text, agentId);
      respondPropose("deny");
      return;
    }
    const q = pendingQuestionBlock();
    if (!q) return;
    if (q.allowFreeText) {
      // Answer: the typed text IS the answer. Never ALSO steer it — the model
      // would see it twice.
      answerQuestion(q.id ?? "", text, null, false);
      return;
    }
    // Fixed choices only: skip the question and steer the text instead, as the
    // instruction the user would rather give.
    enqueueSteer(sid, text, agentId);
    answerQuestion(q.id ?? "", "", null, true);
  }

  // ---- composer ↑/↓ input history (inputHistory.ts) ----------------------
  /** 懒构建该会话的输入历史(首次按 ↑/↓ 或首次发送时调用一次):种子 =
   *  store 已重建的 user 消息文本。payloadFromBlocks 对 subagent 注入/纯附件
   *  消息产出的 text 为空,seedEntries 滤空即天然排除,无需块类型黑名单。 */
  function historyFor(sid: string): HistoryState {
    const st = inputHistoryRef.current[sid];
    if (st) return st;
    const seeded = initHistory(
      seedEntries(
        (storeRef.current[sid] ?? [])
          .filter((m) => m.role === "user")
          .map((m) => m.sendPayload?.text ?? ""),
      ),
    );
    inputHistoryRef.current[sid] = seeded;
    return seeded;
  }

  function send() {
    const text = input.trim();
    const readyFiles = fileAttachments.filter((f) => !f.uploading);
    // In-flight gate, image half (pre-existing bug, both plain sends and
    // steers): readImage is async, so `attachments` lags a just-dropped image.
    // Sending now would silently drop it. Visible hint instead of a no-op.
    if (imagesReading > 0) {
      showComposerHint(tc("composer.hintImagesProcessing"));
      return;
    }
    if (!session) {
      // Home: dropped-file chips are intentionally deferred until the session
      // exists (pendingDocsRef / pendingPathsRef) — they must NOT count as
      // "upload in flight", or send dead-locks on the very uploads it triggers
      // (2026-08-19: 新建会话拖入图片永远发不出去). createAndSend flushes them.
      if (!text && attachments.length === 0 && fileAttachments.length === 0) return;
      void createAndSend({ text, images: attachments, files: readyFiles });
      return;
    }
    const sid = session.id;
    // Parked at a card: the user is answering or changing their mind, not
    // queueing (design §3.4). Checked BEFORE the busy gate because a parked
    // turn is still busy but must never receive a queue entry. (Answering is
    // deliberately not gated on attachments — it carries text only.)
    if (parked) {
      if (!text) return;
      inputHistoryRef.current[sid] = appendEntry(historyFor(sid), text);
      handleParkedSend(sid, text, target ?? session.agent_id ?? g.agents[0]?.id ?? null);
      setInput("");
      setMenu(null);
      return;
    }
    // In-flight gate, document half: a chip still uploading is not in
    // `readyFiles`, so this used to return SILENTLY — send appeared dead. Show
    // why instead (brief §3).
    if (readyFiles.length !== fileAttachments.length) {
      showComposerHint(tc("composer.hintFileUploading"));
      return;
    }
    if (busyBySessionRef.current[sid]) {
      // A turn is running → this is steering, not a new turn: queue it for the
      // next superstep of the turn that is already going (design §3.1). Text
      // AND attachments ride the steer (steer-attachments v2) — with nothing to
      // send, do nothing.
      if (!text && attachments.length === 0 && readyFiles.length === 0) return;
      inputHistoryRef.current[sid] = appendEntry(historyFor(sid), text);
      enqueueSteer(
        sid,
        text,
        target ?? session.agent_id ?? g.agents[0]?.id ?? null,
        attachments,
        readyFiles,
      );
      setInput("");
      setAttachments([]);
      setFileAttachments([]);
      setTarget(null);
      setMenu(null);
      return;
    }
    if (!text && attachments.length === 0 && readyFiles.length === 0) return;
    const agentId = target ?? session.agent_id ?? g.agents[0]?.id ?? null;
    if (agentId && agentId !== session.agent_id) g.setSessionAgent(session.id, agentId);
    // Final prune against the raw (untrimmed) input, deduped — only mentions
    // whose @kind:label token is still present are sent. The server treats this
    // structured list as authoritative (text tokens are its raw-client fallback).
    const mentions = dedupeMentions(pruneMentions(mentionsRef.current[sid] ?? [], input));
    inputHistoryRef.current[sid] = appendEntry(historyFor(sid), text);
    // Not connected? No silent no-op — the bubble is still created and lands
    // in the failed state (red ❗) so the send attempt always has a visible
    // outcome and can be retried.
    attemptSend(sid, { text, images: attachments, files: readyFiles, mentions, agentId });
    setInput("");
    setAttachments([]);
    setFileAttachments([]);
    setTarget(null);
    setMenu(null);
    mentionsRef.current[sid] = [];
  }

  /** Lazy creation: home composer send creates the session, flushes buffered
   *  attachments, awaits the socket, then posts the turn. */
  async function createAndSend(payload: {
    text: string;
    images: Attachment[];
    files: FileAttachment[];
  }) {
    if (homeCreatingRef.current) return;
    homeCreatingRef.current = true;
    // Fire 即清空 composer——必须赶在 g.newSession 之前(同步)。engine 的会话
    // 切换 effect([session?.id])在 home→会话时把「当次渲染的 input/附件」存进
    // HOME_SLOT 草稿槽；newSession 内部 setActiveSessionId 触发它时若 composer
    // 还没清(原实现清在 await 链末尾，effect 闭包里是旧值)，已发送的文本就被
    // 存成首页草稿——回到新建对话，发过的内容重新出现(用户报告 2026-10-15)。
    // 先清则 effect 存进槽的是空值；未发送文本的 ⌘N 往返保留草稿是特性，不受
    // 影响。创建失败/异常按原样恢复(保持「banner 报错、composer 留字」契约)。
    const prevInput = input;
    const prevImages = attachments;
    const prevFiles = fileAttachments;
    const restoreComposer = () => {
      setInput(prevInput);
      setAttachments(prevImages);
      setFileAttachments(prevFiles);
    };
    setInput("");
    setAttachments([]);
    setFileAttachments([]);
    try {
      const agentId = target ?? g.agents[0]?.id ?? null;
      const s = await g.newSession(agentId, {
        ...(homeModel ?? {}),
        // 出生即挂载：pending 目录随建会话请求提交(后端 create_session 原生支持)
        context_folders: pendingMounts.ids.length ? pendingMounts.ids : undefined,
        primary_folder: pendingMounts.primary ?? undefined,
      });
      if (!s) {
        restoreComposer(); // sessionError banner carries the reason
        return;
      }
      curSessionIdRef.current = s.id;
      connectSession(s.id);
      const docs = pendingDocsRef.current;
      pendingDocsRef.current = [];
      const natives = pendingPathsRef.current;
      pendingPathsRef.current = [];
      // Collect the resolved entries so they ride in the turn payload — the
      // fileAttachments state update above hasn't rendered yet in this closure.
      const flushed: FileAttachment[] = [];
      for (const d of docs) {
        const e = await uploadOneDoc(s.id, d.file, d.tmpId);
        if (e) flushed.push(e);
      }
      for (const n of natives) {
        const e = await attachOne(s.id, n.path, n.tmpId);
        if (e) flushed.push(e);
      }
      // The socket race fix: await open instead of failing fast. Timeout
      // degrades to attemptSend's retryable failed bubble.
      await waitForSocketOpen(s.id);
      attemptSend(s.id, {
        ...payload,
        files: [...payload.files, ...flushed],
        mentions: [],
        agentId,
      });
      setTarget(null);
      setMenu(null);
      // 下一个新会话按库里的 auto_mount 条目重新预填(手动改动不跨会话残留)。
      void resetPendingMounts();
    } catch (e) {
      // 兜底：发送链上的任何异常都不吞用户文本——原实现清空在末尾，异常时
      // 文本天然保留；清空前移后在这里补齐同等保证。
      restoreComposer();
      throw e;
    } finally {
      homeCreatingRef.current = false;
    }
  }

  return { send, historyFor, inputHistoryRef };
}
