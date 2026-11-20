"use client";

/**
 * usePinSessionLoop —— Pin 浮窗的单会话消息环（重构批次 3-4）。
 *
 * 定位：useSessionTurn 的 Pin 形态。opts 接口与 components/session/
 * useSessionTurn 逐字对齐（sessionId/midPrefix/mapHistory/mergeWithLive/
 * onExtraEvent/onTurnStart/onTurnEnd/onTurnStopped/onSocketReopen/texts/
 * buildErrorMsg），差异只在内部实现的三处 Pin 语义（见下）——共享层
 * applyBlock 超集化（sessionBlocks.applyBlock → 直调 streamCore）之后，
 * 本文件可退化为 `return useSessionTurn(opts)` 的薄包装，那是对共享层的
 * 单点决策，留给父会话（TODO 见批次报告）。
 *
 * 为什么不直接用 useSessionTurn（三处语义差异，均已用脚本实证）：
 * ① 流帧归约：useSessionTurn.mutateLive 硬调 sessionBlocks.applyBlock
 *   （旧语义：tool.end 无 id 时 name 兜底不限 pending 从头扫取最早同名块、
 *   name-only 空结果 filter 全删同名、delta 只在末尾同 kind 才合并）——
 *   批次 3-1（#240）修的三 bug 会全部回退。这里保持 pinApplyBlock 直调
 *   主版 streamCore.applyBlock（2026-11-15 tool.end 定位 + 交叉增量合并）。
 * ② turn.state 处置：共享版无 sendSeq>probeSeq 守卫——探针在途时 turn 收
 *   口、flushSteerQueue 立刻发新 turn 的话，迟到的 running=false 回答会把
 *   新 turn 判死。这里走 pinTurnStateAction（resume/stale/idle/reconcile）。
 * ③ believing-live 周期探针：共享 socket 层只有 onopen 单次探针；live ws
 *   上丢收尾帧会永远卡「pending 气泡 + 冻结计时」（fe0d06e/a1618f7 修的
 *   正是它）。这里按 pinWatchdogTick 周期直发（bug ③ 的守卫语义）。
 *
 * 已消费的共享层（真消重，语义为超集或逐字同构）：
 * - useSessionSocket：connect/3s 重连/20s ping/45s 静默强断（10s 巡检）、
 *   frame_session 归属守卫、malformed 帧吞掉。onopen 的无 seq 探针被
 *   显式关闭（isTurnInFlight 缺省），改由本 hook 自发并记 probeSeq。
 * - sessionBlocks：makeMid/newTurnId/finalizePendingTools/mapHistoryToMsgs。
 */

import { useEffect, useRef, useState } from "react";
import { getSessionHistory } from "@/lib/runtime";
import { hasPendingTool, type Block } from "@/components/chat/blocks";
import {
  finalizePendingTools,
  makeMid,
  newTurnId,
  type PermissionPrompt,
  type SessionFrame,
  type SessionHistoryResponse,
  type SessionMsg,
  SOCKET_TIMING,
} from "@/components/session/sessionBlocks";
import { useSessionSocket, type SocketStatus } from "@/components/session/useSessionSocket";
import { pinApplyBlock, pinTurnStateAction, pinWatchdogTick } from "@/components/pin/pinStreamCore";

/** 消费方事件回调拿到的消息环操作面（与 useSessionTurn.TurnEventApi 同构）。 */
export interface PinTurnApi<Msg extends SessionMsg> {
  ensureLive: () => string;
  mutateLive: (ev: SessionFrame) => void;
  addSystemRow: (text: string) => void;
  closeLive: (finalizeTools: boolean) => void;
  setMessages: React.Dispatch<React.SetStateAction<Msg[]>>;
  setPermission: React.Dispatch<React.SetStateAction<PermissionPrompt | null>>;
  mid: () => string;
}

export interface PinPostTurnArgs {
  /** 发给服务端的 message 文本。 */
  text: string;
  /** 乐观 user 气泡的 blocks（Pin：file/image/text 组合）。 */
  userBlocks: Block[];
  /** invoke 载荷的多模态图片（{data, media_type}）/ 已上传文档。 */
  images?: Array<{ data: string; media_type: string }>;
  files?: Array<{ id: string; name: string; path: string }>;
  notReadyLabel: () => string;
  failedLabel: () => string;
  /** 已发出（清 input/附件/重置输入框高度）。turnId 是本端乐观生成的
   *  turn id——消费方的 steer 队列要用它指向在途 turn（发到 turn.start
   *  到达之间的窗口期）。 */
  onSent?: (turnId: string) => void;
  /** 发送竞态失败（socket 在检查与发送之间翻到 CLOSING）：乐观气泡已回滚、
   *  onSent 已清走内容——消费方必须把 text/附件放回 composer。 */
  onFailed?: (text: string) => void;
}

export interface UsePinSessionLoopOpts<Msg extends SessionMsg> {
  sessionId: string;
  midPrefix: string;
  /** /history → Msg[]（薄包装 sessionBlocks.mapHistoryToMsgs）；mid 由 hook 注入。 */
  mapHistory: (res: SessionHistoryResponse, mid: () => string) => Msg[];
  /** 首载/reconcile 的合并策略（Pin：mergeHistoryWithLive，填缺不覆盖）。 */
  mergeWithLive?: (hist: Msg[], live: Msg[]) => Msg[];
  /** 公共 case 之外的事件（steer.* / context.* / history.cleared / team.*…）。 */
  onExtraEvent?: (ev: SessionFrame, api: PinTurnApi<Msg>) => void;
  /** 每帧钩子（含公共 case 帧）——Pin 的「压缩中…」瞬态清零需要看见流帧。 */
  onFrame?: (ev: SessionFrame) => void;
  onTurnStart?: (ev: SessionFrame, liveId: string, api: PinTurnApi<Msg>) => void;
  onTurnEnd?: () => void;
  onTurnStopped?: () => void;
  onSocketReopen?: (send: (frame: unknown) => void) => void;
  texts: {
    interruptedTool: () => string;
    unknownError: () => string;
    compacted: (count: number) => string;
  };
  buildErrorMsg?: (text: string, api: PinTurnApi<Msg>) => Msg;
}

export function usePinSessionLoop<Msg extends SessionMsg>(opts: UsePinSessionLoopOpts<Msg>) {
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
  const asMsg = (m: SessionMsg): Msg => m as Msg;

  // ── 探针时序守卫（批次 3-1 语义 ②）──
  // ioSeq 单调计数「谁在这条 socket 上最后说话」；probe 发出记 probeSeq，
  // invoke 真正发出记 sendSeq。探针回答只描述服务端处理探针那一刻的会话态：
  // 此后又发过新 turn（sendSeq > probeSeq）时 running=false 不能据此
  // reconcile，否则把刚发出的 turn 判死。
  const ioSeqRef = useRef(0);
  const probeSeqRef = useRef(0);
  const sendSeqRef = useRef(0);
  // onopen 探针的 6s 无应答兜底（旧 runtime）；turn.state 到达即取消。
  const reconcileFallbackRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 周期探针的 lastSeen（每帧刷新；45s 静默强断本身由共享 socket 层持有）。
  const lastSeenRef = useRef(0);

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
      prev.map((m) => (m.id === id ? { ...m, blocks: pinApplyBlock(m.blocks, ev) } : m)),
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
      ...prev, asMsg({ id: mid(), role: "system", blocks: [{ kind: "context", text }] }),
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

  const api: PinTurnApi<Msg> = { ensureLive, mutateLive, addSystemRow, closeLive, setMessages, setPermission, mid };

  function handle(ev: SessionFrame) {
    lastSeenRef.current = Date.now();
    optsRef.current.onFrame?.(ev);
    switch (ev.event) {
      case "turn.start": {
        busyRef.current = true;
        setBusy(true);
        setSendError(null);
        const id = ensureLive();
        optsRef.current.onTurnStart?.(ev, id, api);
        break;
      }
      case "token.delta":
      case "thinking.delta":
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
        // 内建命令回执 / busy 提示——落进 live 气泡。
        mutateLive({ event: "token.delta", content: (ev.message as string) || "" });
        break;
      case "context.compacted":
        addSystemRow(optsRef.current.texts.compacted(Number(ev.compacted_messages ?? 0)));
        break;
      case "turn.state":
        // 探针应答（6s 兜底已取消）。决策在 pinTurnStateAction（纯函数，
        // 单测钉住）：resume → 流照常续；stale → 回答描述的是探针那一刻，
        // 此后本端又发过新 turn，等流事件自愈；idle → 本端已不信 turn 在途，
        // 无事可做；reconcile → turn 已不在而本地仍 live：从 checkpoint 重建。
        if (reconcileFallbackRef.current) {
          clearTimeout(reconcileFallbackRef.current);
          reconcileFallbackRef.current = null;
        }
        if (
          pinTurnStateAction(
            { running: ev.running },
            {
              believingLive: Boolean(liveRef.current || busyRef.current),
              sendSeq: sendSeqRef.current,
              probeSeq: probeSeqRef.current,
            },
          ) === "reconcile"
        ) {
          reconcileFromHistory();
          busyRef.current = false;
          setBusy(false);
        }
        break;
      case "message.end":
        closeLive(false);
        // 回合已结束：滞留的权限提示作废（可能已被主窗口应答，服务端
        // _PENDING_RESUME 会去重）。
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
    // 共享层的 onopen 探针不记 probeSeq（stale 守卫失明），显式关闭，
    // 由下面的 onOpen 自发——语义 ② 的落点。
    onOpen: (sendOnSock) => {
      setSendError(null);
      optsRef.current.onSocketReopen?.(sendOnSock);
      if (liveRef.current || busyRef.current) {
        // 重连落在 turn 中途：探询 turn 是否还在（记 probeSeq）；无应答
        // （旧 runtime）6s 后兜底 reconcile。
        probeSeqRef.current = ++ioSeqRef.current;
        sendOnSock({ type: "turn_state" });
        if (reconcileFallbackRef.current) clearTimeout(reconcileFallbackRef.current);
        reconcileFallbackRef.current = setTimeout(() => {
          reconcileFallbackRef.current = null;
          reconcileFromHistory();
        }, SOCKET_TIMING.RECONCILE_FALLBACK_MS);
      }
    },
  });

  // 首载 history：快照可能晚于 connect() 的实时帧（Issue #16），经
  // mergeWithLive 填缺，不覆盖已渲染的 live 帧。
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

  // believing-live 周期探针（批次 3-1 语义 ③）：收尾帧在 live ws 上丢失时
  // 静默看门狗永远不触发（ping 有应答不算静默），前端会永远停在「pending
  // 气泡 + 冻结计时」。10s 一探，回答走上面 turn.state 的三态处置。
  // close 半边（45s 静默强断）语义相同且已由共享 socket 层持有，此处不重复。
  useEffect(() => {
    const iv = setInterval(() => {
      const { probe } = pinWatchdogTick({
        now: Date.now(),
        lastSeen: lastSeenRef.current,
        believingLive: Boolean(liveRef.current || busyRef.current),
        socketOpen: socket.isOpen(),
      });
      if (probe) {
        probeSeqRef.current = ++ioSeqRef.current;
        socket.send({ type: "turn_state" });
      }
    }, SOCKET_TIMING.WATCH_TICK_MS);
    return () => clearInterval(iv);
    // socket.send/isOpen 读 hook 内 sockRef，引用稳定。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 卸载清理自设的探针兜底（socket 本体由 useSessionSocket 的 cleanup 关）。
  useEffect(
    () => () => {
      if (reconcileFallbackRef.current) clearTimeout(reconcileFallbackRef.current);
    },
    [],
  );

  /** 发一个真 turn：gate（busy/socket）→ 乐观 user+live 气泡 → invoke 发帧
   *  → 失败整体回滚。发送/失败文案与 composer 恢复由消费方注入。
   *  !isOpen 的 notReady 路径不调 onFailed（对齐共享版：此时 onSent 未执行、
   *  composer 内容未清）。 */
  function postTurn(args: PinPostTurnArgs): boolean {
    if (busyRef.current) return false;
    if (!socket.isOpen()) {
      setSendError(args.notReadyLabel());
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
    args.onSent?.(turnId);
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
    // 只有真正发出的 invoke 才盖戳——turn.state 回答若早于它（probeSeq 更
    // 小）不得裁这个 turn（stale 守卫的另一半）。
    sendSeqRef.current = ++ioSeqRef.current;
    return true;
  }

  function stopTurn() {
    socket.send({ type: "stop" });
  }

  /** 权限应答（plain permission_response；重连会重发提示）。steer 队列的
   *  resume-before-respond 规则是消费方逻辑，在调用本函数前自行处理。 */
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
    /** 安全发帧（boolean 返回；UI 文案归消费方）。 */
    sendFrame: socket.send,
    isOpen: socket.isOpen,
    postTurn,
    stopTurn,
    respond,
    // 消息环原语（onExtraEvent 的 api 已覆盖；此处供消费方特殊路径直用）
    ensureLive,
    mutateLive,
    closeLive,
    addSystemRow,
    reconcileFromHistory,
    mid,
  };
}
