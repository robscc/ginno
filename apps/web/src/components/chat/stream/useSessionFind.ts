"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useGinno } from "@/lib/store";
import {
  buildFindIndex,
  searchableBlockText,
  searchFindIndex,
  type FindHit,
} from "@/lib/sessionFind";
import { useChatT, useEventI18nText } from "../blocks/i18n";
import { planUpdatePresentation, searchableReportText } from "../blocks/subagentCards";
import { contextDisplayText, stripSources } from "../blocks/sources";
import { isReportRow, planUpdateNeedsAttention } from "../blocks/reportFold";
import type { ChatMsg } from "../streamCore";
import type { useTranscriptProjection } from "./TranscriptList";
import * as painter from "./findHighlight";

export interface SessionFindState {
  open: boolean;
  query: string;
  hits: FindHit[];
  activeIdx: number;
  setQuery: (q: string) => void;
  next: () => void;
  prev: () => void;
  close: () => void;
  focusRequest: number;
}

export function useSessionFind(
  messages: ChatMsg[],
  projection: ReturnType<typeof useTranscriptProjection>,
  sessionId: string | undefined,
  scrollRef: React.RefObject<HTMLElement | null>,
  originalMessages: ChatMsg[],
): SessionFindState {
  const [open, setOpen] = useState(false);
  const [query, updateQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState<{ query: string; hit: FindHit }>();
  const [jumpRequest, setJumpRequest] = useState(0);
  const [focusRequest, setFocusRequest] = useState(0);
  const { sessions } = useGinno();
  const { tc, tr } = useChatT();
  const eventText = useEventI18nText();

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 150);
    return () => clearTimeout(timer);
  }, [query]);

  const index = useMemo(() => {
    if (!open) return [];
    const sessionsById = new Map(sessions.map((session) => [session.id, session]));
    return buildFindIndex(messages, projection, (block, row) => {
      switch (block.kind) {
        case "context":
          return contextDisplayText(block, eventText);
        case "plan_update": {
          const compact = (row.role === "assistant" || isReportRow(row)) && !planUpdateNeedsAttention(block);
          const { title, progress, items, notes, settled, id } = planUpdatePresentation(block, compact, { tc, tr });
          return [title, progress, id, ...items.flatMap((item) => [item.id, item.label, item.snippet]), settled, notes]
            .filter(Boolean).join("\n");
        }
        case "subagent_result":
          return searchableReportText(block, sessionsById.get(block.sessionId), tc);
        case "text":
          return row.role === "assistant" ? stripSources(block.text) : block.text;
        default:
          return searchableBlockText(block);
      }
    }, originalMessages);
  }, [open, messages, projection, sessions, eventText, tc, tr, originalMessages]);
  const hits = useMemo(() => searchFindIndex(index, debounced), [index, debounced]);
  const activeIdx = selected?.query === debounced ? Math.max(0, hits.findIndex(
    (hit) => hit.sourceKey === selected.hit.sourceKey && hit.occurrence === selected.hit.occurrence,
  )) : 0;
  const active = hits[activeIdx];
  const paintState = useRef({ hits, activeIdx, debounced });
  paintState.current = { hits, activeIdx, debounced };
  const paint = useCallback(() => {
    const { hits, activeIdx, debounced } = paintState.current;
    if (scrollRef.current) painter.paint(scrollRef.current, hits, activeIdx, debounced);
  }, [scrollRef]);

  useEffect(() => {
    if (open) paint();
  }, [open, hits, activeIdx, paint]);
  useEffect(() => {
    if (!open) return;
    window.addEventListener("ginno:transcript-slices-settled", paint);
    return () => {
      window.removeEventListener("ginno:transcript-slices-settled", paint);
      painter.clear();
    };
  }, [open, sessionId, paint]);

  const { sourceKey, rowKey, blockIdx, occurrence } = active ?? {};
  useEffect(() => {
    const scroll = scrollRef.current;
    if (!open || !scroll || sourceKey === undefined || rowKey === undefined || blockIdx === undefined || occurrence === undefined) return;
    return painter.focusActiveHit(scroll, { rowKey, blockIdx, occurrence }, debounced, paint);
  }, [open, sourceKey, rowKey, blockIdx, occurrence, debounced, jumpRequest, scrollRef, paint]);

  useEffect(() => {
    updateQuery("");
    setDebounced("");
    setSelected(undefined);
    setOpen(false);
  }, [sessionId]);

  useEffect(() => {
    const onOpen = () => {
      if (!sessionId) return;
      setOpen(true);
      setFocusRequest((n) => n + 1);
    };
    window.addEventListener("ginno:find-open", onOpen);
    return () => window.removeEventListener("ginno:find-open", onOpen);
  }, [sessionId]);

  const jump = (idx: number) => {
    if (hits.length === 0) return;
    const bounded = ((idx % hits.length) + hits.length) % hits.length;
    setSelected({ query: debounced, hit: hits[bounded] });
    setJumpRequest((n) => n + 1);
  };
  const close = () => {
    setOpen(false);
    updateQuery("");
    setDebounced("");
    setSelected(undefined);
  };

  const setQuery = (value: string) => {
    updateQuery(value);
    if (value.trim() !== query.trim()) setSelected(undefined);
  };

  return { open, query, hits, activeIdx, setQuery,
    next: () => jump(activeIdx + 1), prev: () => jump(activeIdx - 1), close, focusRequest };
}
