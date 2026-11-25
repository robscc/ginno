"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject, RefObject } from "react";
import { consumeMessageTarget, flashMessageAnchor } from "@/components/shell/searchShared";
import { FIND_REVEAL_ROW } from "@/lib/sessionFind";
import {
  SLICE_INITIAL_BLOCKS, SLICE_STEP_BLOCKS, clampSkip, sliceResetNeeded, sliceStart,
} from "@/lib/transcriptSlices";
import type { foldReportRows } from "../blocks/reportFold";
import { failedTurnBannerVisible, type ChatMsg } from "../streamCore";

const WINDOW_ROWS = 80;
const WINDOW_STEP = 60;
const TARGET_CONTEXT = 8;

interface ReadingWindow {
  sessionId: string | undefined;
  firstKey: string;
  start: number;
  target: number;
}

function rowElement(scroll: HTMLElement, key: string) {
  return scroll.querySelector<HTMLElement>(`[data-find-row="${CSS.escape(key)}"]`);
}

export function useTranscriptWindow({
  sessionId, messages, folded, rowKeys, scrollRef, stickRef, latestWindowRef, onWindowEndChange,
}: {
  sessionId: string | undefined;
  messages: ChatMsg[];
  folded: ReturnType<typeof foldReportRows<ChatMsg>>;
  rowKeys: string[];
  scrollRef: RefObject<HTMLDivElement | null>;
  stickRef: MutableRefObject<boolean>;
  latestWindowRef: MutableRefObject<(() => void) | null>;
  onWindowEndChange: (hasLater: boolean) => void;
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
  const committed = useRef<{ sid: string | undefined; firstKey: string; atEnd: boolean; length: number }>();
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
  } else if (view && later && stickRef.current && previous?.sid === sessionId && previous?.atEnd) {
    // Reaching the real bottom resumes following on the next append.
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
  const pendingMessage = useRef<{ id: string; sid: string | undefined; until: number } | null>(null);

  useLayoutEffect(() => {
    committed.current = { sid: sessionId, firstKey: keyAt(start), atEnd: later === 0, length: indices.length };
    onWindowEndChange(later > 0);
    const scroll = scrollRef.current;
    if (!scroll) return;
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
      if (from && scroll && !stickRef.current) {
        const top = scroll.getBoundingClientRect().top;
        for (const row of scroll.querySelectorAll<HTMLElement>("[data-find-row]")) {
          const rect = row.getBoundingClientRect();
          if (rect.bottom > top && row.dataset.findRow) {
            prependAnchor.current = { key: row.dataset.findRow, top: rect.top };
            break;
          }
        }
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
    pendingMessage.current = null;
    open(nextStart, overlap);
  };

  const reveal = (key: string) => {
    const target = indices.findIndex((index) => rowKeys[index] === key);
    if (target < 0) return;
    stickRef.current = false;
    if (target >= start + from && target < start + to) return;
    const nextStart = target >= start && target < end ? start :
      Math.max(0, Math.min(tailStart, target - Math.floor(WINDOW_ROWS / 2)));
    open(nextStart, target);
  };

  const handlePendingMessage = () => {
    const target = pendingMessage.current;
    if (!target) return;
    if (target.sid !== sessionId || Date.now() >= target.until) {
      pendingMessage.current = null;
      return;
    }
    const scroll = scrollRef.current;
    const source = messages.findIndex((message) => message.id === target.id);
    if (!scroll || source < 0) return;
    const host = folded.absorbed.has(source) ? folded.origins.findIndex((origins) =>
      origins.some((origin) => origin.row === source),
    ) : source;
    if (host < 0) return;
    const key = rowKeys[host];
    const row = rowElement(scroll, key);
    if (!row) {
      reveal(key);
      return;
    }
    pendingMessage.current = null;
    stickRef.current = false;
    row.scrollIntoView({ block: "center" });
    flashMessageAnchor(row);
  };
  const actions = useRef({ reveal, handlePendingMessage });
  actions.current = { reveal, handlePendingMessage };

  useLayoutEffect(() => {
    // Pending ID requests survive asynchronous history and the commit that
    // mounts the requested row. Once landed, scrolling belongs to the reader.
    if (pendingMessage.current) actions.current.handlePendingMessage();
  });

  useLayoutEffect(() => {
    latestWindowRef.current = () => {
      pendingMessage.current = null;
      position.current = null;
      prependAnchor.current = null;
      setReading(null);
    };
    return () => { latestWindowRef.current = null; };
  }, [latestWindowRef]);

  useEffect(() => {
    setReading((current) => current?.sessionId === sessionId ? current : null);
    const jump = (id: string) => {
      pendingMessage.current = { id, sid: sessionId, until: Date.now() + 3000 };
      actions.current.handlePendingMessage();
    };
    const onMessage = (event: Event) => {
      const id = (event as CustomEvent<string>).detail;
      if (id) jump(id);
    };
    const onFind = (event: Event) => {
      const { rowKey, scrollEl } = (event as CustomEvent<{ rowKey: string; scrollEl: HTMLElement }>).detail;
      if (scrollEl !== scrollRef.current) return;
      pendingMessage.current = null;
      actions.current.reveal(rowKey);
    };
    window.addEventListener("ginno:scroll-to-message", onMessage);
    window.addEventListener(FIND_REVEAL_ROW, onFind);
    const stored = consumeMessageTarget();
    if (stored) jump(stored);
    return () => {
      window.removeEventListener("ginno:scroll-to-message", onMessage);
      window.removeEventListener(FIND_REVEAL_ROW, onFind);
    };
  }, [sessionId, scrollRef]);

  return { rendered, earlier: start, later, move };
}
