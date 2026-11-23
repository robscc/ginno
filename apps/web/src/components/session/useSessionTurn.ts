"use client";

/**
 * useSessionTurn —— 单会话流「消息环」（批次 3-2 从 PinStream/DevSessionDrawer
 * 吸收）：live 气泡生命周期、turn 事件公共分发、/history 首载与 reconcile、
 * 发 turn 的乐观更新与回滚；组合 useSessionSocket。
 *
 * 公共事件（turn.start / 流帧 / permission / notice / context.compacted /
 * turn.state / message.end / turn.stopped / error）在此消化；消费方差异
 * （Dev：version.propose / run.* / workflows.changed；Pin：steer / team.*）
 * 经 onExtraEvent 注入，拿 TurnEventApi 继续操作消息环。PinStream 待 3-1
 * 合并后接入——onTurnStart/onTurnEnd/onTurnStopped/mergeWithLive 是预留接缝。
 */

import { useEffect, useRef, useState } from "react";
import { getSessionHistory } from "@/lib/runtime";
import { hasPendingTool, type Block } from "@/components/chat/blocks";
import {
  applyBlock,
  finalizePendingTools,
  makeMid,
  newTurnId,
  type PermissionPrompt,
  type SessionFrame,
  type SessionHistoryResponse,
  type SessionMsg,
} from "./sessionBlocks";
import { useSessionSocket, type SocketStatus } from "./useSessionSocket";

/** 扩展 case 拿到的消息环操作面（与 hook 内部同函数，无二次封装）。 */
export interface TurnEventApi<Msg extends SessionMsg> {
  /** 事件 → live 气泡（共享 applyBlock）。 */
  mutateLive: (ev: SessionFrame) => void;
  /** 追加居中系统行（压缩回执 / 团队投递回执…）。 */
  addSystemRow: (text: string) => void;
  /** 收口 live 气泡；finalizeTools=true 强制收尾 pending 工具气泡。 */
  closeLive: (finalizeTools: boolean) => void;
  setMessages: React.Dispatch<React.SetStateAction<Msg[]>>;
  mid: () => string;
}

import { MAX_SEND_TEXT_BYTES, textByteLength } from "@/lib/limits";

export interface PostTurnArgs {
  /** 发给服务端的 message 文本。 */
  text: string;
  /** 乐观 user 气泡的 blocks（Dev：纯 text；Pin：file/image/text 组合）。 */
  userBlocks: Block[];
  /** invoke 载荷的多模态图片 / 已上传文档。 */
  images?: Array<{ data: string; media_type: string }>;
  files?: Array<{ id: string; name: string; path: string }>;
  notReadyLabel: () => string;
  failedLabel: () => string;
  /** 文本超 WS 上限（D11）时的行内提示。 */
  tooLongLabel: () => string;
  /** 已发出（清 input/附件/重置输入框高度）；失败回滚后（放回 composer）。 */
  onSent?: () => void;
  onFailed?: (text: string) => void;
}

export interface UseSessionTurnOpts<Msg extends SessionMsg> {
  sessionId: string;
  /** 消息 id 前缀（pm/dm，调试可辨来源）。 */
  midPrefix: string;
  /** /history → Msg[]（薄包装 mapHistoryToMsgs）；mid 由 hook 注入，与消息环同一计数器。 */
  mapHistory: (res: SessionHistoryResponse, mid: () => string) => Msg[];
  /** 首载/reconcile 的合并策略。缺省 = 历史覆盖（Dev）；Pin 传 mergeHistoryWithLive（填缺不覆盖）。 */
  mergeWithLive?: (hist: Msg[], live: Msg[]) => Msg[];
  /** 公共 case 之外的事件（version.propose / run.* / steer / team.*…）。 */
  onExtraEvent?: (ev: SessionFrame, api: TurnEventApi<Msg>) => void;
  /** 消费方附加动作：turn.start（Pin 记服务端 turn_id）/ message.end+error
   *  （Pin flushSteerQueue）/ turn.stopped（Pin recallSteers）/ socket 重连
   *  （Pin 重发 steer 队列）。 */
  onTurnStart?: (ev: SessionFrame, liveId: string) => void;
  onTurnEnd?: () => void;
  onTurnStopped?: () => void;
  onSocketReopen?: (send: (frame: unknown) => void) => void;
  /** 文案注入：中断工具收尾 / error 兜底 / context.compacted 系统行。 */
  texts: {
    interruptedTool: () => string;
    unknownError: () => string;
    compacted: (count: number) => string;
  };
  /** error 卡构造（Pin 带 retryText）；缺省基座卡。 */
  buildErrorMsg?: (text: string, api: TurnEventApi<Msg>) => Msg;
}

export function useSessionTurn<Msg extends SessionMsg>(opts: UseSessionTurnOpts<Msg>) {
  const { sessionId } = opts;
  const [messages, setMessages] = useState<Msg[]>([]);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [permission, setPermission] = useState<PermissionPrompt | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const liveRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const midRef = useRef(makeMid(opts.midPrefix));
  // 回调/文案经 ref 转发：socket 事件（可能发生在任意渲染之后）读最新实现。
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const mid = () => midRef.current();
  // 泛型 Msg（Pin 扩展 teamReply/retryText）在 hook 内以基座构造、收窄返回。
  const asMsg = (m: SessionMsg): Msg => m as Msg;

  function ensureLive(): string {
    if (liveRef.current) return liveRef.current;
    const id = mid();
    liveRef.current = id;
    setLiveId(id);
    setMessages((prev) => [...prev, asMsg({ id, role: "assistant", blocks: [] })]);
    return id;
  }

  function mutateLive(ev: SessionFrame) {
    const id = ensureLive();
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, blocks: applyBlock(m.blocks, ev) } : m)),
    );
  }

  /** 收口 live 气泡。空 live 气泡（首 token 前结束）直接丢弃；finalizeTools
   *  时 pending 工具气泡强制收尾（stop/error 路径，服务端不会再发 tool.end）。 */
  function closeLive(finalizeTools: boolean) {
    const id = liveRef.current;
    liveRef.current = null;
    setLiveId(null);
    busyRef.current = false;
    setBusy(false);
    setMessages((prev) => {
      let next = prev;
      if (id) {
        next = next.filter((m) => !(m.id === id && m.blocks.length === 0));
        if (finalizeTools) {
          const label = optsRef.current.texts.interruptedTool();
          next = next.map((m) =>
            hasPendingTool(m.blocks)
              ? { ...m, blocks: finalizePendingTools(m.blocks, label) }
              : m,
          );
        }
      }
      return next;
    });
  }

  function addSystemRow(text: string) {
    setMessages((prev) => [
      ...prev,
      asMsg({ id: mid(), role: "system", blocks: [{ kind: "context", text }] }),
    ]);
  }

  function reconcileFromHistory() {
    getSessionHistory(sessionId)
      .then((res) => {
        const hist = optsRef.current.mapHistory(res, mid);
        const merge = optsRef.current.mergeWithLive;
        setMessages((prev) => (merge ? merge(hist, prev) : hist));
      })
      .catch(() => {
        /* sidecar down — the reconnect loop will retry */
      });
  }

  const api: TurnEventApi<Msg> = { mutateLive, addSystemRow, closeLive, setMessages, mid };

  function handle(ev: SessionFrame) {
    switch (ev.event) {
      case "turn.start": {
        busyRef.current = true;
        setBusy(true);
        setSendError(null);
        const id = ensureLive();
        optsRef.current.onTurnStart?.(ev, id);
        break;
      }
      case "token.delta":
      case "thinking.delta":
      case "reasoning.summary":
      case "tool.start":
      case "tool.args":
      case "tool.end":
      case "image.emit":
        busyRef.current = true;
        setBusy(true);
        mutateLive(ev);
        break;
      case "permission.request":
        setPermission({ tool: ev.tool as string, args: ev.args });
        break;
      case "notice":
        // 内建命令回执 / busy 提示——落进 live 气泡（两消费方同款）。
        mutateLive({ event: "token.delta", content: (ev.message as string) || "" });
        break;
      case "context.compacted":
        addSystemRow(optsRef.current.texts.compacted(Number(ev.compacted_messages ?? 0)));
        break;
      case "turn.state":
        if (ev.running && ev.reasoning_summary) mutateLive({ ...ev, event: "reasoning.summary", summary: ev.reasoning_summary });
        // 探针应答（6s 兜底已在 socket 层取消）：turn 不在了 → 本地 live 态
        // 是陈旧的，从持久化 history 重建。
        if (ev.running) break;
        reconcileFromHistory();
        busyRef.current = false;
        setBusy(false);
        break;
      case "message.end":
        if (ev.reasoning_summary && liveRef.current) mutateLive({ ...ev, event: "reasoning.summary", summary: ev.reasoning_summary });
        closeLive(false);
        // 回合已结束：滞留的权限提示作废（可能已被主窗口应答，
        // 服务端 _PENDING_RESUME 会去重）。
        setPermission(null);
        optsRef.current.onTurnEnd?.();
        break;
      case "turn.stopped":
        closeLive(true);
        setPermission(null);
        optsRef.current.onTurnStopped?.();
        break;
      case "error": {
        closeLive(true);
        setPermission(null);
        const text = String(ev.message || "") || optsRef.current.texts.unknownError();
        const msg = optsRef.current.buildErrorMsg
          ? optsRef.current.buildErrorMsg(text, api)
          : asMsg({ id: mid(), role: "assistant", error: true, blocks: [{ kind: "text", text }] });
        setMessages((prev) => [...prev, msg]);
        optsRef.current.onTurnEnd?.();
        break;
      }
      default:
        optsRef.current.onExtraEvent?.(ev, api);
        break;
    }
  }

  const socket = useSessionSocket({
    sessionId,
    onEvent: handle,
    isTurnInFlight: () => liveRef.current != null || busyRef.current,
    onOpen: (send) => {
      setSendError(null);
      optsRef.current.onSocketReopen?.(send);
    },
    onReconcileTimeout: reconcileFromHistory,
  });

  // 首载 history：快照可能晚于 connect() 的实时帧（Issue #16），需要合并的
  // 消费方经 mergeWithLive 填缺，否则直接替换（mount 时 prev 本就是空）。
  useEffect(() => {
    let alive = true;
    getSessionHistory(sessionId)
      .then((res) => {
        if (!alive) return;
        const hist = optsRef.current.mapHistory(res, mid);
        const merge = optsRef.current.mergeWithLive;
        setMessages((prev) => (merge ? merge(hist, prev) : hist));
      })
      .catch(() => {
        /* sidecar down — events will populate once it returns */
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  /** 发一个真 turn：gate（busy/socket）→ 乐观 user+live 气泡 → invoke 发帧
   *  → 失败整体回滚。发送/失败文案与 composer 恢复由消费方注入。 */
  function postTurn(args: PostTurnArgs): boolean {
    if (busyRef.current) return false;
    if (!socket.isOpen()) {
      setSendError(args.notReadyLabel());
      return false;
    }
    // 超大文本前置拦截（D11）：与后端 WS 1MB 帧上限同口径。
    if (textByteLength(args.text) > MAX_SEND_TEXT_BYTES) {
      setSendError(args.tooLongLabel());
      return false;
    }
    const turnId = newTurnId();
    const uid = mid();
    const lid = mid();
    busyRef.current = true;
    setBusy(true);
    liveRef.current = lid;
    setLiveId(lid);
    setSendError(null);
    setMessages((prev) => [
      ...prev,
      asMsg({ id: uid, role: "user", blocks: args.userBlocks, turnId }),
      asMsg({ id: lid, role: "assistant", blocks: [] }),
    ]);
    args.onSent?.();
    const ok = socket.send({
      type: "invoke",
      message: args.text,
      agent_id: null,
      turn_id: turnId,
      images: args.images ?? [],
      files: args.files ?? [],
    });
    if (!ok) {
      // socket 在检查与发送之间翻到 CLOSING：整体回滚。
      busyRef.current = false;
      setBusy(false);
      liveRef.current = null;
      setLiveId(null);
      setMessages((prev) => prev.filter((m) => m.id !== lid && m.id !== uid));
      args.onFailed?.(args.text);
      setSendError(args.failedLabel());
      return false;
    }
    return true;
  }

  function stopTurn() {
    socket.send({ type: "stop" });
  }

  /** 权限/提案应答（plain permission_response，无 propose_id——服务端按
   *  会话跟踪 pending 中断并用 {decision} 恢复；重连会重发提示）。 */
  function respond(decision: "allow" | "deny") {
    socket.send({ type: "permission_response", decision });
    setPermission(null);
  }

  return {
    messages,
    setMessages,
    liveId,
    busy,
    permission,
    setPermission,
    sendError,
    setSendError,
    wsStatus: socket.status as SocketStatus,
    sendFrame: socket.send,
    postTurn,
    stopTurn,
    respond,
    // 消息环原语（onExtraEvent 的 api 已覆盖；此处供消费方组件特殊路径直用）
    ensureLive,
    mutateLive,
    closeLive,
    addSystemRow,
    reconcileFromHistory,
    mid,
  };
}
