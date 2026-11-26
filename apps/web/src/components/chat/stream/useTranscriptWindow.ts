"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { HistoryLoadStatus } from "../engine/history";
import type { HistoryTarget } from "@/lib/api/sessions";
import type { MutableRefObject, RefObject } from "react";
import { consumeMessageTarget, flashMessageAnchor } from "@/components/shell/searchShared";
import { focusActiveHit } from "./findHighlight";
import { FIND_REVEAL_ROW } from "@/lib/sessionFind";
import {
  SLICE_INITIAL_BLOCKS, SLICE_STEP_BLOCKS, clampSkip, sliceResetNeeded, sliceStart,
} from "@/lib/transcriptSlices";
import type { foldReportRows } from "../blocks/reportFold";
import { failedTurnBannerVisible, type ChatMsg } from "../streamCore";

const WINDOW_ROWS = 80;
const WINDOW_STEP = 60;
const TARGET_CONTEXT = 8;
const READING_CACHE_LIMIT = 32;

export interface ReadingAnchor {
  id: string;
  offset: number;
}

export interface TranscriptViewport {
  scroll: (nearTail: boolean) => void;
  cancelRestore: () => void;
  latest: () => void;
}

interface ReadingRestore extends ReadingAnchor {
  request?: AbortController;
  resolved?: boolean;
}

interface ReadingWindow {
  sessionId: string | undefined;
  firstKey: string;
  start: number;
  target: number;
}

function rowElement(scroll: HTMLElement, key: string) {
  return scroll.querySelector<HTMLElement>(`[data-find-row="${CSS.escape(key)}"]`);
}

function firstVisibleRow(scroll: HTMLElement, selector: string) {
  const { top, bottom } = scroll.getBoundingClientRect();
  for (const row of scroll.querySelectorAll<HTMLElement>(selector)) {
    const rect = row.getBoundingClientRect();
    if (rect.top >= bottom) break;
    if (rect.bottom > top && rect.height > 0) return { row, top: rect.top, offset: rect.top - top };
  }
}

export function useTranscriptWindow({
  sessionId, messages, folded, rowKeys, scrollRef, stickRef, viewportRef, onWindowEndChange,
  loadHistory, historyStatus, readingCache, focusLatestRef,
}: {
  sessionId: string | undefined;
  messages: ChatMsg[];
  folded: ReturnType<typeof foldReportRows<ChatMsg>>;
  rowKeys: string[];
  scrollRef: RefObject<HTMLDivElement | null>;
  stickRef: MutableRefObject<boolean>;
  viewportRef: MutableRefObject<TranscriptViewport | null>;
  readingCache: Map<string, ReadingAnchor>;
  focusLatestRef: MutableRefObject<string | null>;
  historyStatus: HistoryLoadStatus;
  onWindowEndChange: (hasLater: boolean) => void;
  loadHistory: (sid: string, target: string, signal?: AbortSignal) => Promise<HistoryTarget | null>;
}) {
  // Absorbed reports use their host row, and obsolete failure banners are hidden.
  const indices = useMemo(() => folded.rows.flatMap((row, i) =>
    folded.absorbed.has(i) || (row.error && !failedTurnBannerVisible(messages, i)) ? [] : [i],
  ), [folded, messages]);
  const [reading, setReading] = useState<ReadingWindow | null>(null);
  const view = reading?.sessionId === sessionId ? reading : null;
  const keyAt = (index: number) => rowKeys[indices[index]];
  const tailStart = Math.max(0, indices.length - WINDOW_ROWS);
  const savedStart = view ? indices.findIndex((index) => rowKeys[index] === view.firstKey) : -1;
  const start = view ? Math.min(savedStart < 0 ? view.start : savedStart, tailStart) : tailStart;
  const end = Math.min(start + WINDOW_ROWS, indices.length);
  const size = end - start;
  const later = indices.length - end;
  const counts = indices.slice(start, end).map((index) => folded.rows[index].blocks.length);
  const countsRef = useRef(counts);
  countsRef.current = counts;

  // Start tail visits at the end. For jumps, mount the target and its immediate
  // context first, then fill both sides with the same block and row budgets.
  const [slice, setSlice] = useState({ sid: sessionId, view, from: 0, to: WINDOW_ROWS });
  const previousLength = useRef(0);
  const committed = useRef<{ sid: string | undefined; firstKey: string; view: ReadingWindow | null; atEnd: boolean; length: number }>();
  const previous = committed.current;
  // The tail normally follows new rows. If the reader has scrolled back and
  // an append would discard recent history, keep the same rows in place.
  let preservedSlice = false;
  if (!view && !stickRef.current && previous && previous.sid === sessionId && previous.length < indices.length) {
    const priorStart = indices.findIndex((index) => rowKeys[index] === previous.firstKey);
    if (priorStart >= 0 && priorStart < start) {
      const held = { sessionId, firstKey: previous.firstKey, start: priorStart, target: 0 };
      setReading(held);
      setSlice({ ...slice, view: held });
      preservedSlice = true;
    }
  } else if (view && later && stickRef.current && previous?.view === view && previous.atEnd) {
    // Resume following only from the displayed window. A final scroll event
    // from the tail can arrive before a newly requested history window commits.
    setReading(null);
  }
  const freshTail = !view && sliceResetNeeded(
    { sid: slice.sid, len: previousLength.current }, sessionId, indices.length,
  );
  if (!preservedSlice && (slice.view !== view || slice.sid !== sessionId || freshTail)) {
    const target = view?.target;
    const initialEnd = target === undefined ? size : Math.min(size, target + TARGET_CONTEXT + 1);
    const initialStart = sliceStart(counts, initialEnd, SLICE_INITIAL_BLOCKS);
    setSlice({ sid: sessionId, view, from: target === undefined ? initialStart : Math.min(target, initialStart),
      to: view ? initialEnd : WINDOW_ROWS });
  }
  previousLength.current = indices.length;
  const to = Math.min(size, slice.to);
  const from = clampSkip(slice.from, to);
  const rendered = indices.slice(start + from, start + to);
  const prependAnchor = useRef<{ key: string; top: number } | null>(null);
  const position = useRef<{ key: string; edge: ScrollLogicalPosition; focus: boolean } | null>(null);
  const restore = useRef<ReadingRestore>();
  const pendingMessage = useRef<{ id: string; sid: string | undefined; until: number; block?: number | null } | null>(null);

  // TranscriptList is keyed by session. Claim the viewport before the tail
  // pin or a queued scroll event can overwrite the previous visit's anchor.
  useLayoutEffect(() => {
    const saved = sessionId ? readingCache.get(sessionId) : undefined;
    if (sessionId && focusLatestRef.current === sessionId) readingCache.delete(sessionId);
    else if (saved) restore.current = { ...saved };
    stickRef.current = !restore.current;
    return () => {
      restore.current?.request?.abort();
      restore.current = undefined;
    };
  }, [sessionId, readingCache, focusLatestRef, stickRef]);

  useLayoutEffect(() => {
    committed.current = { sid: sessionId, firstKey: keyAt(start), view, atEnd: later === 0, length: indices.length };
    onWindowEndChange(later > 0);
    const scroll = scrollRef.current;
    if (!scroll || restore.current) return;
    if (position.current) {
      const { key, edge, focus } = position.current;
      position.current = null;
      const row = rowElement(scroll, key);
      row?.scrollIntoView({ block: edge });
      if (focus) row?.focus({ preventScroll: true });
    } else if (prependAnchor.current) {
      // Use the retained row's measured position. Chromium may already have
      // compensated via native scroll anchoring, so only apply any remainder.
      const { key, top } = prependAnchor.current;
      const row = rowElement(scroll, key);
      if (row) scroll.scrollTop += row.getBoundingClientRect().top - top;
    }
    prependAnchor.current = null;
  });

  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (scroll && !later && stickRef.current) scroll.scrollTop = scroll.scrollHeight;
  }, [start, from, to, later, sessionId, scrollRef, stickRef]);

  useEffect(() => {
    if (from === 0 && to === size) return;
    const id = setTimeout(() => {
      const scroll = scrollRef.current;
      if (from && scroll && !stickRef.current && !restore.current) {
        const visible = firstVisibleRow(scroll, "[data-find-row]");
        const key = visible?.row.dataset.findRow;
        if (visible && key) prependAnchor.current = { key, top: visible.top };
      }
      setSlice((current) => {
        const counts = countsRef.current;
        if (from) return { ...current, from: sliceStart(counts, from, SLICE_STEP_BLOCKS) };
        const remaining = counts.slice(to).reverse();
        return { ...current, to: counts.length - sliceStart(remaining, remaining.length, SLICE_STEP_BLOCKS) };
      });
    }, 16);
    return () => clearTimeout(id);
  }, [from, to, size, view, sessionId, scrollRef, stickRef]);

  useEffect(() => {
    if (from === 0 && to === size) window.dispatchEvent(new Event("ginno:transcript-slices-settled"));
  }, [from, to, size, view, sessionId]);

  const open = (nextStart: number, target: number) => {
    prependAnchor.current = null;
    stickRef.current = false;
    setReading({ sessionId, firstKey: keyAt(nextStart), start: nextStart, target: target - nextStart });
  };

  const move = (direction: -1 | 1, focus: boolean) => {
    const nextStart = Math.max(0, Math.min(tailStart, start + direction * WINDOW_STEP));
    const overlap = direction < 0 ? start : Math.max(start, end - 1);
    position.current = { key: keyAt(overlap), edge: direction < 0 ? "end" : "start", focus };
    cancelRestore();
    pendingMessage.current = null;
    open(nextStart, overlap);
  };

  const reveal = (key: string) => {
    const target = indices.findIndex((index) => rowKeys[index] === key);
    if (target < 0) return false;
    stickRef.current = false;
    if (target >= start + from && target < start + to) return true;
    const nextStart = target >= start && target < end ? start :
      Math.max(0, Math.min(tailStart, target - Math.floor(WINDOW_ROWS / 2)));
    open(nextStart, target);
    return true;
  };

  const pendingPage = useRef<{ sid: typeof sessionId; key: string; length: number; focus: boolean }>();
  const moveAfterLoad = (focus: boolean) => {
    cancelRestore();
    const intent = { sid: sessionId, key: keyAt(start), length: indices.length, focus };
    stickRef.current = false;
    pendingPage.current = intent;
    return () => { if (pendingPage.current === intent) pendingPage.current = undefined; };
  };
  useLayoutEffect(() => {
    const page = pendingPage.current;
    if (!page || page.sid !== sessionId || indices.length === page.length) return;
    pendingPage.current = undefined;
    const overlap = indices.findIndex((index) => rowKeys[index] === page.key);
    if (overlap < 0) return;
    position.current = { key: page.key, edge: "end", focus: page.focus };
    open(Math.max(0, Math.min(tailStart, overlap - WINDOW_STEP)), overlap);
  });

  const messageRow = (id: string) => {
    const source = messages.findIndex((message) => message.id === id);
    const host = folded.absorbed.has(source) ? folded.origins.findIndex((origins) =>
      origins.some((origin) => origin.row === source),
    ) : source;
    return { source, host, key: rowKeys[host] };
  };

  const handlePendingMessage = () => {
    const target = pendingMessage.current;
    if (!target) return;
    if (target.sid !== sessionId || Date.now() >= target.until) {
      pendingMessage.current = null;
      return;
    }
    const scroll = scrollRef.current;
    const { source, host, key } = messageRow(target.id);
    if (!scroll || !key) return;
    const row = rowElement(scroll, key);
    if (!row) {
      reveal(key);
      return;
    }
    pendingMessage.current = null;
    stickRef.current = false;
    row.scrollIntoView({ block: "center" });
    flashMessageAnchor(row);
    if (target.block != null) {
      const blockIdx = folded.origins[host].findIndex((origin) => origin.row === source && origin.block === target.block);
      if (blockIdx >= 0) cancelToolReveal.current = focusActiveHit(scroll, { rowKey: key, blockIdx, occurrence: 0 }, "", () => {});
    }
  };
  const cancelToolReveal = useRef<() => void>();
  const cancelRestore = () => {
    const attempt = restore.current;
    if (!attempt) return;
    restore.current = undefined;
    attempt.request?.abort();
    if (sessionId) readingCache.delete(sessionId);
  };

  const latest = () => {
    cancelRestore();
    if (sessionId) readingCache.delete(sessionId);
    pendingMessage.current = null;
    pendingPage.current = undefined;
    position.current = null;
    prependAnchor.current = null;
    cancelToolReveal.current?.();
    setReading(null);
  };

  const captureReading = () => {
    const scroll = scrollRef.current;
    if (!sessionId || !scroll || restore.current || pendingMessage.current || historyStatus.kind !== "ready") return;
    if (stickRef.current) {
      readingCache.delete(sessionId);
      return;
    }
    const visible = firstVisibleRow(scroll, "[data-reading-anchor]");
    const id = visible?.row.dataset.msgId;
    if (!visible || !id) return;
    readingCache.delete(sessionId);
    readingCache.set(sessionId, { id, offset: visible.offset });
    if (readingCache.size > READING_CACHE_LIMIT) {
      const oldest = readingCache.keys().next().value;
      if (oldest) readingCache.delete(oldest);
    }
  };

  const [, redrawRestore] = useState(0);
  const restoreReading = () => {
    const attempt = restore.current;
    const scroll = scrollRef.current;
    if (!attempt || !sessionId || !scroll || historyStatus.kind !== "ready") return;
    const fallBack = () => {
      latest();
      stickRef.current = true;
      scroll.scrollTop = scroll.scrollHeight;
    };
    const { key } = messageRow(attempt.id);
    if (key) {
      const row = rowElement(scroll, key);
      if (!row) {
        if (!reveal(key)) fallBack();
        return;
      }
      const rect = row.getBoundingClientRect();
      if (!rect.height) {
        fallBack();
        return;
      }
      // Keep the row visible if a fold collapsed or the viewport shrank.
      const offset = Math.max(1 - rect.height, Math.min(attempt.offset, scroll.clientHeight - 1));
      scroll.scrollTop += rect.top - scroll.getBoundingClientRect().top - offset;
      prependAnchor.current = null;
      if (from === 0 && to === size) restore.current = undefined;
      return;
    }
    if (attempt.resolved) {
      fallBack();
      return;
    }
    if (attempt.request) return;
    const controller = new AbortController();
    attempt.request = controller;
    void loadHistory(sessionId, attempt.id, controller.signal).catch(() => null).then((host) => {
      if (restore.current !== attempt) return;
      if (host) attempt.id = host.id;
      attempt.resolved = true;
      redrawRestore((version) => version + 1);
    });
  };

  const actions = useRef({ reveal, handlePendingMessage, cancelRestore, latest, captureReading });
  actions.current = { reveal, handlePendingMessage, cancelRestore, latest, captureReading };

  useLayoutEffect(() => {
    // Pending ID requests survive asynchronous history and the commit that
    // mounts the requested row. Once landed, scrolling belongs to the reader.
    if (pendingMessage.current) handlePendingMessage();
    if (restore.current) restoreReading();
    else if (!stickRef.current) captureReading();
  });

  useLayoutEffect(() => {
    viewportRef.current = {
      latest: () => actions.current.latest(),
      cancelRestore: () => actions.current.cancelRestore(),
      scroll: (nearTail) => {
        if (restore.current) return;
        stickRef.current = nearTail;
        actions.current.captureReading();
      },
    };
    return () => { viewportRef.current = null; };
  }, [stickRef, viewportRef]);

  useLayoutEffect(() => {
    const jump = (id: string) => {
      actions.current.cancelRestore();
      cancelToolReveal.current?.();
      pendingMessage.current = { id, sid: sessionId, until: Infinity };
      const request = pendingMessage.current;
      if (sessionId) void loadHistory(sessionId, id).then((host) => {
        if (pendingMessage.current !== request) return;
        if (!host) { pendingMessage.current = null; return; }
        request.id = host.id;
        request.block = host.block;
        request.until = Date.now() + 3000;
        actions.current.handlePendingMessage();
      }).catch(() => { if (pendingMessage.current === request) pendingMessage.current = null; });
      actions.current.handlePendingMessage();
    };
    const onMessage = (event: Event) => {
      const id = (event as CustomEvent<string>).detail;
      if (id) jump(id);
    };
    const onFind = (event: Event) => {
      const { rowKey, scrollEl } = (event as CustomEvent<{ rowKey: string; scrollEl: HTMLElement }>).detail;
      if (scrollEl !== scrollRef.current) return;
      actions.current.cancelRestore();
      cancelToolReveal.current?.();
      pendingMessage.current = null;
      actions.current.reveal(rowKey);
    };
    window.addEventListener("ginno:scroll-to-message", onMessage);
    window.addEventListener(FIND_REVEAL_ROW, onFind);
    const stored = sessionId ? consumeMessageTarget() : null;
    if (stored) jump(stored);
    return () => {
      pendingMessage.current = null;
      cancelToolReveal.current?.();
      window.removeEventListener("ginno:scroll-to-message", onMessage);
      window.removeEventListener(FIND_REVEAL_ROW, onFind);
    };
  }, [sessionId, scrollRef, loadHistory]);

  return { rendered, earlier: start, later, move, moveAfterLoad };
}
