"use client";

// Report-fold attention resolver (回报折叠方案 A, 2026-11-25).
//
// The pure rules live in reportFold.ts (vitest, node environment). This hook
// binds them to the store: whether a subagent result needs the user depends on
// the child session's live status, which only the store knows. Grouping
// (processGroups) and the header counts (attentionAfter) both take this same
// resolver, so "not folded" and "counted in the header" can never drift apart.

import { useCallback } from "react";
import { useGinno } from "@/lib/store";
import { reportAttention, type ReportBlock } from "./reportFold";

export function useReportAttention() {
  const g = useGinno();
  return useCallback(
    (block: ReportBlock) =>
      reportAttention(
        block,
        block.kind === "subagent_result"
          ? g.sessions.find((s) => s.id === block.sessionId)?.subagent?.status
          : undefined,
      ),
    [g.sessions],
  );
}
