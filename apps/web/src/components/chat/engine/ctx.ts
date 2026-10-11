// EngineCtx — useChatStreamEngine 拆分出的子模块（engine/handle、engine/steer、
// engine/actions、engine/connect）共用的上下文类型：deps 全量 + hook 作用域里的
// 共享 ref + syncDisplay。子模块函数一律以 (ctx, …) 首参取用，主文件每 render
// 组装一次 ctx，闭包语义与拆分前逐 render 重建的函数声明一致。

import type { ResolvedMention, Trigger, MenuItem } from "@/components/chat/commandMenu";
import type { WorkflowRun } from "@/lib/types";
import type {
  Attachment,
  ChatMsg,
  FileAttachment,
  PermissionPrompt,
  VersionPropose,
} from "../streamCore";
import type { EngineDeps } from "../useChatStreamEngine";

export type WsStatus = "connecting" | "live" | "reconnecting" | "offline";
export interface MenuState { items: MenuItem[]; active: number; trigger: Trigger }
export interface ProposeResult { decision: "allow" | "deny"; workflowId: string; fromVersion: number }

export interface EngineCtx extends EngineDeps {
  // i18n 镜像 ref（socket 回调闭包生命周期长于单次 render，翻译句柄经 ref 保新）。
  i18nRef: {
    current: {
      tr: (key: string, values?: Record<string, string | number>) => string;
      evText: (
        ev: { i18n_key?: unknown; params?: unknown; [k: string]: unknown } | null | undefined,
        fallback: string,
      ) => string;
    };
  };
  liveIdRef: { current: string | null };
  activeSidRef: { current: string | null };
  socketReadyRef: {
    current: Record<
      string,
      { promise: Promise<void>; resolve: () => void; reject: (e: unknown) => void }
    >;
  };
  prevSlotRef: { current: string | null };
  curSessionIdRef: { current: string | null };
  storeRef: { current: Record<string, ChatMsg[]> };
  liveBySessionRef: { current: Record<string, string | null> };
  socketsRef: { current: Record<string, WebSocket> };
  statusRef: { current: Record<string, WsStatus> };
  permsRef: { current: Record<string, PermissionPrompt | null> };
  proposeRef: { current: Record<string, VersionPropose | null> };
  busyBySessionRef: { current: Record<string, boolean> };
  hintTimerRef: { current: ReturnType<typeof setTimeout> | null };
  serverRunningRef: { current: Record<string, boolean> };
  compactingBySessionRef: { current: Record<string, boolean> };
  streamAgentRef: { current: Record<string, string | null> };
  orphanStreamRef: { current: Record<string, boolean> };
  // 孤儿流是否已认领服务端 turn id（见 mutateLive 的认领逻辑）
  adoptedTurnRef: { current: Record<string, boolean> };
  // auto-retry 瞬态提示当前落在 live 气泡里的精确文本块（含分隔符）。
  retryNoticeRef: { current: Record<string, string | null> };
  seenTurnStartRef: { current: Record<string, boolean> };
  pingTimerRef: { current: Record<string, ReturnType<typeof setInterval> | null> };
  watchTimerRef: { current: Record<string, ReturnType<typeof setInterval> | null> };
  reconcileTimerRef: { current: Record<string, ReturnType<typeof setTimeout> | null> };
  reconnTimerRef: { current: Record<string, ReturnType<typeof setTimeout> | null> };
  lastSeenRef: { current: Record<string, number> };
  ioSeqRef: { current: number };
  probeSeqRef: { current: Record<string, number> };
  sendSeqRef: { current: Record<string, number> };
  draftCacheRef: {
    current: Record<
      string,
      { input: string; attachments: Attachment[]; files?: FileAttachment[]; mentions?: ResolvedMention[] }
    >;
  };
  mentionsRef: { current: Record<string, ResolvedMention[]> };
  runsBySessionRef: { current: Record<string, WorkflowRun[]> };
  pendingDocsRef: { current: Array<{ file: File; tmpId: string }> };
  pendingPathsRef: { current: Array<{ path: string; tmpId: string }> };
  // Push the given session's ref state into React display state (main-file定义,
  // 经 ctx 注入——后台会话的 store 写入靠它跳过显示同步)。
  syncDisplay: (sid: string) => void;
}
