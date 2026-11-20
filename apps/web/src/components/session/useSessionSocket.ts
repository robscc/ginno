"use client";

/**
 * useSessionSocket —— 单会话 WS 生命周期 hook（批次 3-2 从 PinStream /
 * DevSessionDrawer 逐字同构的 connect() 吸收；多会话形态的
 * useChatStreamEngine 不在此列）。四常数见 SOCKET_TIMING（sessionBlocks）。
 *
 * 归属守卫：frame_session 不属于本会话的帧一律丢弃并告警（对齐 PinStream
 * 2026-10-01 渲染串线报告；Dev 抽屉此前缺此守卫，接入后一并获得）。
 * 回调经 optsRef 转发——socket handler 闭包一次成型，永远读到消费方最新实现。
 */

import { useEffect, useRef, useState } from "react";
import { openSessionSocket } from "@/lib/runtime";
import { SOCKET_TIMING, type SessionFrame } from "./sessionBlocks";

export type SocketStatus = "connecting" | "live" | "reconnecting";

export { SOCKET_TIMING };

export interface UseSessionSocketOpts {
  sessionId: string;
  /** 每帧事件（已 JSON.parse、已过 frame_session 守卫）。 */
  onEvent: (ev: SessionFrame) => void;
  /** 消费方自报「turn 在途」——决定 onopen 是否发 turn_state 探针 + 6s 兜底。 */
  isTurnInFlight?: () => boolean;
  /** 探针 6s 无应答（旧 runtime）的兜底——消费方 reconcileFromHistory。 */
  onReconcileTimeout?: () => void;
  /** socket（重）连成功；send 是绑定本 socket 的发帧器（Pin 重发 steer 队列用）。 */
  onOpen?: (send: (frame: unknown) => void) => void;
}

export function useSessionSocket(opts: UseSessionSocketOpts) {
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const sockRef = useRef<WebSocket | null>(null);
  const reconnRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const watchRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconcileRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSeenRef = useRef(0);
  const optsRef = useRef(opts);
  optsRef.current = opts;

  function connect() {
    const existing = sockRef.current;
    if (
      existing &&
      (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    if (reconnRef.current) {
      clearTimeout(reconnRef.current);
      reconnRef.current = null;
    }
    setStatus("connecting");
    const sid = optsRef.current.sessionId;
    const sock = openSessionSocket(sid);
    sockRef.current = sock;
    sock.onopen = () => {
      if (sockRef.current !== sock) return;
      setStatus("live");
      lastSeenRef.current = Date.now();
      const sendOnSock = (frame: unknown) => {
        if (sock.readyState !== WebSocket.OPEN) return;
        try {
          sock.send(JSON.stringify(frame));
        } catch {
          /* ignore */
        }
      };
      optsRef.current.onOpen?.(sendOnSock);
      if (optsRef.current.isTurnInFlight?.()) {
        // 重连落在 turn 中途：探询 turn 是否还在；无应答（旧 runtime）走兜底。
        sendOnSock({ type: "turn_state" });
        if (reconcileRef.current) clearTimeout(reconcileRef.current);
        reconcileRef.current = setTimeout(() => {
          reconcileRef.current = null;
          optsRef.current.onReconcileTimeout?.();
        }, SOCKET_TIMING.RECONCILE_FALLBACK_MS);
      }
      pingRef.current = setInterval(() => {
        if (sock.readyState === WebSocket.OPEN) {
          try {
            sock.send(JSON.stringify({ type: "ping" }));
          } catch {
            /* ignore */
          }
        }
      }, SOCKET_TIMING.PING_MS);
      watchRef.current = setInterval(() => {
        if (Date.now() - lastSeenRef.current > SOCKET_TIMING.SILENCE_CLOSE_MS) {
          try {
            sock.close();
          } catch {
            /* ignore */
          }
        }
      }, SOCKET_TIMING.WATCH_TICK_MS);
    };
    sock.onmessage = (e) => {
      if (sockRef.current !== sock) return;
      lastSeenRef.current = Date.now();
      try {
        const ev = JSON.parse(e.data) as SessionFrame & { frame_session?: string };
        // Frame-ownership guard（同 PinStream/useChatStreamEngine）。
        if (ev.frame_session && sid && ev.frame_session !== sid) {
          console.warn(
            `[ginno] dropping cross-session frame event=${ev.event} frame_session=${ev.frame_session} socket_session=${sid}`,
          );
          return;
        }
        // 探针应答到达：取消 6s 兜底（协议层语义，消费方 handle 无需感知）。
        if (ev.event === "turn.state" && reconcileRef.current) {
          clearTimeout(reconcileRef.current);
          reconcileRef.current = null;
        }
        optsRef.current.onEvent(ev);
      } catch {
        /* malformed frame — ignore */
      }
    };
    sock.onerror = () => {
      if (sockRef.current !== sock) return;
      try {
        sock.close();
      } catch {
        /* ignore */
      }
    };
    sock.onclose = () => {
      if (pingRef.current) {
        clearInterval(pingRef.current);
        pingRef.current = null;
      }
      if (watchRef.current) {
        clearInterval(watchRef.current);
        watchRef.current = null;
      }
      if (sockRef.current !== sock) return;
      sockRef.current = null;
      setStatus("reconnecting");
      reconnRef.current = setTimeout(() => {
        reconnRef.current = null;
        connect();
      }, SOCKET_TIMING.RECONNECT_MS);
    };
  }

  // 生命周期与消费方组件一致：parent 按 sessionId key 重挂载，socket 在
  // cleanup 关闭且 detach（onclose 不得再排重连）。
  useEffect(() => {
    connect();
    return () => {
      if (reconnRef.current) clearTimeout(reconnRef.current);
      if (pingRef.current) clearInterval(pingRef.current);
      if (watchRef.current) clearInterval(watchRef.current);
      if (reconcileRef.current) clearTimeout(reconcileRef.current);
      reconnRef.current = pingRef.current = watchRef.current = reconcileRef.current = null;
      const sock = sockRef.current;
      sockRef.current = null;
      if (sock) {
        try {
          sock.close();
        } catch {
          /* ignore */
        }
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.sessionId]);

  /** 安全发帧：socket OPEN 才发；返回是否真的发出（消费方决定失败文案）。 */
  const send = (frame: unknown): boolean => {
    const sock = sockRef.current;
    if (!sock || sock.readyState !== WebSocket.OPEN) return false;
    try {
      sock.send(JSON.stringify(frame));
      return true;
    } catch {
      return false;
    }
  };

  return {
    status,
    send,
    isOpen: () => sockRef.current?.readyState === WebSocket.OPEN,
  };
}
