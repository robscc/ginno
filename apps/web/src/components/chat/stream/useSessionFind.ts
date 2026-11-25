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
import { useTranscriptProjection } from "./TranscriptList";
import { getSessionHistoryIndex, type HistoryResult, type HistoryTarget } from "@/lib/api/sessions";
import type { Block } from "../blocks/types";
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
  indexStatus: "loading" | "error" | "ready";
  retry: () => void;
}

export function useSessionFind({
  messages, projection, sessionId, scrollRef, sourceMessages,
  visibleMessages, mapHistory, loadHistory, historyRevision,
}: {
  messages: ChatMsg[];
  projection: ReturnType<typeof useTranscriptProjection>;
  sessionId: string | undefined;
  scrollRef: React.RefObject<HTMLElement | null>;
  sourceMessages: ChatMsg[];
  visibleMessages: (rows: ChatMsg[]) => ChatMsg[];
  historyRevision: string | undefined;
  mapHistory: (response: HistoryResult) => ChatMsg[];
  loadHistory: (sid: string, target: string) => Promise<HistoryTarget | null>;
}): SessionFindState {
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

  const [snapshot, setSnapshot] = useState<{ sid: string; rows: ChatMsg[] }>();
  const [indexStatus, setIndexStatus] = useState<SessionFindState["indexStatus"]>("loading");
  const [retryCount, setRetryCount] = useState(0);
  useEffect(() => {
    if (!open || !sessionId) return;
    let alive = true;
    setIndexStatus("loading");
    getSessionHistoryIndex(sessionId).then(
      (result) => {
        if (!alive) return;
        setSnapshot({ sid: sessionId, rows: mapHistory(result) });
        setIndexStatus("ready");
      },
      () => { if (alive) setIndexStatus("error"); },
    );
    return () => { alive = false; };
  }, [open, sessionId, retryCount, mapHistory, historyRevision]);

  const searchSource = useMemo(() => {
    if (!open) return [];
    if (!snapshot || snapshot.sid !== sessionId) return sourceMessages;
    const saved = new Set(snapshot.rows.map((row) => row.id));
    // Keep a full snapshot for old results and include new live rows from the
    // current store. Opening a new search reads a new checkpoint.
    return [...snapshot.rows, ...sourceMessages.filter((row) => !saved.has(row.id))];
  }, [open, snapshot, sessionId, sourceMessages]);
  const searchMessages = useMemo(() => visibleMessages(searchSource), [visibleMessages, searchSource]);
  const searchProjection = useTranscriptProjection(searchMessages, sessionId);
  const sessionsById = useMemo(() => new Map(sessions.map((session) => [session.id, session])), [sessions]);
  const displayText = useCallback((block: Block, row: ChatMsg) => {
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
  }, [sessionsById, eventText, tc, tr]);
  const index = useMemo(() => buildFindIndex(searchMessages, searchProjection, displayText, searchSource),
    [searchMessages, searchProjection, displayText, searchSource]);
  const addresses = useMemo(() => new Map((open
    ? buildFindIndex(messages, projection, displayText, sourceMessages) : [])
    .map((entry) => [entry.sourceKey, entry])), [open, messages, projection, displayText, sourceMessages]);

  const hits = useMemo(() => searchFindIndex(index, debounced), [index, debounced]);
  const activeIdx = selected?.query === debounced ? Math.max(0, hits.findIndex(
    (hit) => hit.sourceKey === selected.hit.sourceKey && hit.occurrence === selected.hit.occurrence,
  )) : 0;
  const active = hits[activeIdx];
  const paintState = useRef({ hits, activeIdx, debounced });
  paintState.current = { hits: hits.map((hit) => ({ ...hit, ...addresses.get(hit.sourceKey) })), activeIdx, debounced };
  const paint = useCallback(() => {
    const { hits, activeIdx, debounced } = paintState.current;
    if (scrollRef.current) painter.paint(scrollRef.current, hits, activeIdx, debounced);
  }, [scrollRef]);

  useEffect(() => {
    if (open) paint();
  }, [open, hits, activeIdx, addresses, paint]);
  useEffect(() => {
    if (!open) return;
    window.addEventListener("ginno:transcript-slices-settled", paint);
    return () => {
      window.removeEventListener("ginno:transcript-slices-settled", paint);
      painter.clear();
    };
  }, [open, sessionId, paint]);

  const address = active && addresses.get(active.sourceKey);
  const rowKey = address?.rowKey;
  const blockIdx = address?.blockIdx;
  const occurrence = active?.occurrence;
  const requestId = active && !address
    ? searchMessages[searchProjection.rowKeys.indexOf(active.rowKey)]?.id : undefined;
  useEffect(() => {
    if (!open || !sessionId || !requestId) return;
    let alive = true;
    loadHistory(sessionId, requestId).catch(() => { if (alive) setIndexStatus("error"); });
    return () => { alive = false; };
  }, [open, sessionId, requestId, loadHistory, retryCount]);
  useEffect(() => {
    const scroll = scrollRef.current;
    if (!open || !scroll || rowKey === undefined || blockIdx === undefined || occurrence === undefined) return;
    return painter.focusActiveHit(scroll, { rowKey, blockIdx, occurrence }, debounced, paint);
  }, [open, rowKey, blockIdx, occurrence, debounced, jumpRequest, scrollRef, paint]);

  useEffect(() => {
    updateQuery("");
    setDebounced("");
    setSelected(undefined);
    setOpen(false);
    setSnapshot(undefined);
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
    setSnapshot(undefined);
    updateQuery("");
    setDebounced("");
    setSelected(undefined);
  };

  const setQuery = (value: string) => {
    updateQuery(value);
    if (value.trim() !== query.trim()) setSelected(undefined);
  };

  return { open, query, hits, activeIdx, setQuery,
    next: () => jump(activeIdx + 1), prev: () => jump(activeIdx - 1), close, focusRequest, indexStatus, retry: () => setRetryCount((n) => n + 1) };
}
