"use client";

/**
 * useSessionFind — Cmd+F 会话内搜索的 hook（find-bar-design §3.1）。
 *
 * open/query/hits/activeIdx 状态机 + ginno:find-open 事件监听 + 会话切换
 * reset + 150ms 防抖 + 跳转 dispatch（复用 ⌘K 的 ginno:scroll-to-message
 * 扩窗管线，零新机制）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  buildFindIndex,
  resetFindCache,
  searchFindIndex,
} from "@/lib/sessionFind";
import * as painter from "./findHighlight";

export interface SessionFindState {
  open: boolean;
  query: string;
  hits: ReturnType<typeof searchFindIndex>;
  activeIdx: number;
  setOpen: (v: boolean) => void;
  setQuery: (q: string) => void;
  next: () => void;
  prev: () => void;
  close: () => void;
  focusRequest: number;
}

export function useSessionFind(
  messages: Array<{ id: string; blocks?: Array<Record<string, unknown>> }>,
  liveId: string | null | undefined,
  sessionId: string | undefined,
  scrollRef: React.RefObject<HTMLElement | null>,
): SessionFindState {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const [focusRequest, setFocusRequest] = useState(0);
  const hitsRef = useRef<ReturnType<typeof searchFindIndex>>([]);

  // 防抖 150ms（§3.1）。
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 150);
    return () => clearTimeout(t);
  }, [query]);

  // 索引：buildFindIndex 内部按 msgId+blocks 引用缓存，流式期只重提取 live。
  const index = useMemo(
    () => (open ? buildFindIndex(messages, liveId) : []),
    [open, messages, liveId],
  );
  const hits = useMemo(
    () => (open ? searchFindIndex(index, debounced) : []),
    [open, index, debounced],
  );
  hitsRef.current = hits;

  // query 变化 → 回到首个命中；painter 重刷。
  useEffect(() => {
    setActiveIdx(0);
    if (open && scrollRef.current) {
      painter.paint(scrollRef.current, hits, 0, debounced);
    }
  }, [debounced, hits, open, scrollRef]);

  // 高亮重刷：hits/activeIdx 变（流式增长/遍历）。
  useEffect(() => {
    if (open && scrollRef.current) {
      painter.paint(scrollRef.current, hits, activeIdx, debounced);
    }
  }, [open, hits, activeIdx, debounced, scrollRef]);

  // 关闭/卸载/会话切换：清高亮 + reset（对齐 headKeep 重置模式）。
  useEffect(() => {
    if (!open) painter.clear();
  }, [open]);
  useEffect(() => {
    painter.clear();
    resetFindCache();
    setQuery("");
    setDebounced("");
    setActiveIdx(0);
    setOpen(false);
  }, [sessionId]);
  useEffect(() => () => painter.clear(), []);

  // ⌘F 入口事件（AppShell 转发，§6.1）：打开或聚焦全选。
  useEffect(() => {
    const onOpen = () => {
      if (!sessionId) return; // pin/无会话不响应
      setOpen((v) => {
        if (!v) {
          setQuery("");
          setActiveIdx(0);
        }
        setFocusRequest((n) => n + 1);
        return true;
      });
    };
    window.addEventListener("ginno:find-open", onOpen);
    return () => window.removeEventListener("ginno:find-open", onOpen);
  }, [sessionId]);

  const jump = useCallback(
    (idx: number) => {
      const list = hitsRef.current;
      if (list.length === 0) return;
      const bounded = ((idx % list.length) + list.length) % list.length;
      setActiveIdx(bounded);
      const hit = list[bounded];
      // §4.2：复用 ⌘K 锚点管线（revealUpTo 扩窗 + 居中 + violet ring）。
      window.dispatchEvent(
        new CustomEvent("ginno:scroll-to-message", { detail: hit.msgId }),
      );
      if (scrollRef.current) {
        painter.focusActiveHit(scrollRef.current, hit, debounced);
      }
    },
    [debounced, scrollRef],
  );

  const next = useCallback(() => jump(activeIdx + 1), [jump, activeIdx]);
  const prev = useCallback(() => jump(activeIdx - 1), [jump, activeIdx]);
  const close = useCallback(() => {
    setOpen(false);
    painter.clear();
  }, []);

  return {
    open, query, hits, activeIdx,
    setOpen, setQuery, next, prev, close, focusRequest,
  };
}
