"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useGinno } from "@/lib/store";
import {
  buildFindIndex,
  mergeFindHistory,
  searchableBlockText,
  searchFindIndex,
  type FindHit,
} from "@/lib/sessionFind";
import { useChatT, useEventI18nText } from "../blocks/i18n";
import { planUpdatePresentation, searchableReportText } from "../blocks/subagentCards";
import { contextDisplayText, maskPartialSources, stripSources } from "../blocks/sources";
import { isReportRow, planUpdateNeedsAttention } from "../blocks/reportFold";
import { cleanAgentText, splitInlineWidgets } from "../blocks/inlineWidgets";
import { markdownSearchText } from "@/lib/markdownSearchText";
import type { ChatMsg } from "../streamCore";
import type { HistoryReadOptions } from "../engine/history";
import { useTranscriptProjection } from "./TranscriptList";
import type { HistoryIndex, HistoryResult, HistoryTarget } from "@/lib/api/sessions";
import { FIND_HISTORY_CHANGED, sessionFindCache } from "@/lib/sessionFindCache";
import type { Block } from "../blocks/types";
import * as painter from "./findHighlight";

type TextBlock = Extract<Block, { kind: "text" }>;
const assistantTextCache = new WeakMap<TextBlock, { live: boolean; text: string }>();

function assistantSearchText(block: TextBlock, live: boolean): string {
  const cached = assistantTextCache.get(block);
  if (cached?.live === live) return cached.text;
  const body = stripSources(block.text);
  const parts = splitInlineWidgets(cleanAgentText(live ? maskPartialSources(body) : body), live);
  const prose = parts.flatMap((part) => "text" in part ? [part.text] : []).join(" ");
  const text = parts.flatMap((part) => {
    if ("text" in part) return [markdownSearchText(part.text)];
    const summary = part.widget.summary;
    return summary && !prose.includes(summary) ? [markdownSearchText(summary)] : [];
  }).join("\n");
  assistantTextCache.set(block, { live, text });
  return text;
}

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
  messages, projection, sessionId, scrollRef, sourceMessages, liveId,
  visibleMessages, mapHistory, loadHistory, historyRevision, historyLoading,
}: {
  messages: ChatMsg[];
  projection: ReturnType<typeof useTranscriptProjection>;
  sessionId: string | undefined;
  liveId: string | null;
  scrollRef: React.RefObject<HTMLElement | null>;
  sourceMessages: ChatMsg[];
  visibleMessages: (rows: ChatMsg[]) => ChatMsg[];
  historyRevision: string | undefined;
  historyLoading: boolean;
  mapHistory: (response: HistoryResult) => ChatMsg[];
  loadHistory: (sid: string, target: string, options?: HistoryReadOptions) => Promise<HistoryTarget | null>;
}): SessionFindState {
  const [openSession, setOpenSession] = useState<string>();
  const open = openSession !== undefined && openSession === sessionId;
  const [query, updateQuery] = useState("");
  const [debouncedQuery, setDebounced] = useState("");
  const debounced = open && query.trim() ? debouncedQuery : "";
  const searching = debounced !== "";
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

  const [invalidation, setInvalidation] = useState(0);
  const [retryCount, setRetryCount] = useState(0);
  type ReadScope = { sid: string; revision?: string; invalidation: number; retryCount: number };
  const readScope = useMemo<ReadScope | undefined>(() => searching && !historyLoading && sessionId
    ? { sid: sessionId, revision: historyRevision, invalidation, retryCount } : undefined,
  [searching, historyLoading, sessionId, historyRevision, invalidation, retryCount]);
  const previousRead = useRef<ReadScope>();
  const [indexRead, setIndexRead] = useState<{ scope: ReadScope; response?: HistoryIndex }>();

  useEffect(() => {
    const changed = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== sessionId || !sessionId) return;
      sessionFindCache.invalidate(sessionId);
      setInvalidation((value) => value + 1);
    };
    window.addEventListener(FIND_HISTORY_CHANGED, changed);
    return () => window.removeEventListener(FIND_HISTORY_CHANGED, changed);
  }, [sessionId]);

  useEffect(() => {
    const prior = previousRead.current;
    previousRead.current = readScope;
    if (!readScope) return;
    const { sid, revision } = readScope;
    // Reopening validates against the current checkpoint, since the last page
    // may predate an out-of-band rewrite. A new page can also confirm an index
    // that arrived ahead of that page, without reading it again.
    const verify = prior?.sid !== sid || prior.invalidation !== readScope.invalidation ||
      prior.retryCount !== readScope.retryCount;
    let alive = true;
    sessionFindCache.read(sid, revision, verify).then(
      (response) => { if (alive) setIndexRead({ scope: readScope, response }); },
      () => { if (alive) setIndexRead({ scope: readScope }); },
    );
    return () => { alive = false; };
  }, [readScope]);

  const current = indexRead?.scope === readScope ? indexRead : undefined;
  const snapshot = useMemo(() => current?.response ? mapHistory(current.response) : undefined, [current, mapHistory]);
  const searchSource = useMemo(() => {
    if (!searching) return [];
    return snapshot ? mergeFindHistory(snapshot, sourceMessages, liveId) : sourceMessages;
  }, [searching, snapshot, sourceMessages, liveId]);
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
        return row.role === "assistant" ? assistantSearchText(block, row.id === liveId) : block.text;
      default:
        return searchableBlockText(block);
    }
  }, [sessionsById, eventText, tc, tr, liveId]);
  const index = useMemo(() => buildFindIndex(searchMessages, searchProjection, displayText, searchSource),
    [searchMessages, searchProjection, displayText, searchSource]);
  const addresses = useMemo(() => new Map((searching
    ? buildFindIndex(messages, projection, displayText, sourceMessages) : [])
    .map((entry) => [entry.sourceKey, entry])), [searching, messages, projection, displayText, sourceMessages]);

  const hits = useMemo(() => searchFindIndex(index, debounced), [index, debounced]);
  const selectedIdx = selected?.query === debounced ? hits.findIndex(
    (hit) => hit.sourceKey === selected.hit.sourceKey && hit.occurrence === selected.hit.occurrence,
  ) : 0;
  const activeIdx = Math.max(0, selectedIdx);
  const active = selectedIdx < 0 && !current ? undefined : hits[activeIdx];
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

  const visibleAddress = active && addresses.get(active.sourceKey);
  const indexRevision = current?.response?.revision;
  // The search projection can be newer than the paged rows. Re-read a loaded
  // row if its current preview cannot address a hit from that newer revision.
  const previewNeedsRefresh = visibleAddress && indexRevision && indexRevision !== historyRevision &&
    searchFindIndex([visibleAddress], debounced).length <= active.occurrence;
  const address = previewNeedsRefresh ? undefined : visibleAddress;
  const rowKey = address?.rowKey;
  const blockIdx = address?.blockIdx;
  const occurrence = active?.occurrence;
  const requestId = active && !address
    ? searchMessages[searchProjection.rowKeys.indexOf(active.rowKey)]?.id : undefined;
  const [targetError, setTargetError] = useState<{ scope: ReadScope; id: string }>();
  useEffect(() => {
    if (!readScope || !requestId) return;
    let alive = true;
    loadHistory(readScope.sid, requestId, { indexRevision }).catch(() => {
      if (alive) setTargetError({ scope: readScope, id: requestId });
    });
    return () => { alive = false; };
  }, [readScope, requestId, loadHistory, indexRevision]);
  let indexStatus: SessionFindState["indexStatus"] = searching && historyLoading ? "loading" : "ready";
  if (readScope) {
    if ((targetError?.scope === readScope && targetError.id === requestId) || (current && !current.response)) {
      indexStatus = "error";
    } else if (!current) indexStatus = "loading";
  }
  useEffect(() => {
    const scroll = scrollRef.current;
    if (!open || !scroll || rowKey === undefined || blockIdx === undefined || occurrence === undefined) return;
    return painter.focusActiveHit(scroll, { rowKey, blockIdx, occurrence }, debounced, paint);
  }, [open, rowKey, blockIdx, occurrence, debounced, jumpRequest, scrollRef, paint]);

  useEffect(() => {
    updateQuery("");
    setDebounced("");
    setSelected(undefined);
    setOpenSession(undefined);
    setIndexRead(undefined);
    setTargetError(undefined);
  }, [sessionId]);

  useEffect(() => {
    const onOpen = () => {
      if (!sessionId) return;
      setOpenSession(sessionId);
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
    setOpenSession(undefined);
    setIndexRead(undefined);
    setTargetError(undefined);
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
