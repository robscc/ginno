"use client";

// useSuggestions — V2 P2-1 execution-time suggestions 的状态、加载与动作
// （自 ChatStream.tsx 机械拆出，效果/依赖逐字一致）。

import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { pushNotice } from "@/lib/notice";
import type { useGinno } from "@/lib/store";
import type { SessionMeta } from "@/lib/types";
import type { SendPayload } from "../streamCore";

// V2 P2-1: dismissals are per (session_id, suggestion id), persisted locally.
const SUGGEST_DISMISS_LS_KEY = "ginno-dismissed-suggestions";

export function useSuggestions({
  g,
  session,
  running,
  target,
  attemptSend,
  showComposerHint,
  setModelOpen,
  reportDistill,
}: {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  running: boolean;
  target: string | null;
  attemptSend: (sid: string, payload: SendPayload, userMsgId?: string) => void;
  showComposerHint: (msg: string) => void;
  setModelOpen: Dispatch<SetStateAction<boolean>>;
  reportDistill: (
    r: { ok?: boolean; path?: string; detail?: { error?: string } },
    kind: "skill" | "kb",
  ) => void;
}) {
  const tc = useTranslations("chat");
  const router = useRouter();
  // ── V2 P2-1 execution-time suggestions ──────────────────────────────────
  // Fetched when a session opens and again whenever a turn settles
  // (running → false); see the effects below. Automation sessions are filtered
  // server-side (`excluded: "automation"`).
  const [suggestions, setSuggestions] = useState<api.SessionSuggestion[]>([]);
  // Suggestion id whose action is currently running (button spinner).
  const [suggestBusy, setSuggestBusy] = useState<string | null>(null);
  // Dismissed suggestion keys `${sessionId}:${suggestionId}` (localStorage
  // mirror; read once on mount, written on every dismiss).
  const [suggDismissed, setSuggDismissed] = useState<Record<string, true>>({});

  // ── V2 P2-1 / P2-3 localStorage hydration (client-only) ─────────────────
  // try/catch every read/write: localStorage throws in private windows and can
  // be blocked entirely, and none of this is load-bearing for the chat itself.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(SUGGEST_DISMISS_LS_KEY);
      if (raw) {
        const parsed: unknown = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          setSuggDismissed(parsed as Record<string, true>);
        }
      }
    } catch {
      /* localStorage unavailable — dismissals stay in memory for this mount */
    }
  }, []);

  // Suggestions, load 1/2: once when a session becomes the active one.
  // `cancelled` (not curSessionIdRef) guards late responses — on the very first
  // mount the engine's session effect runs before these, but a rapid switch can
  // still resolve out of order.
  const suggestSid = session?.id ?? null;
  useEffect(() => {
    setSuggestions([]);
    if (!suggestSid) return;
    let cancelled = false;
    void (async () => {
      try {
        const r = await api.getSuggestions(suggestSid);
        if (cancelled) return;
        setSuggestions(
          r.excluded === "automation" || !Array.isArray(r.suggestions) ? [] : r.suggestions,
        );
      } catch {
        /* runtime unreachable — no cards; the next turn end retries */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [suggestSid]);

  // Suggestions, load 2/2: after each turn settles. The cards are actionable
  // only once the turn is over (compact/switch would race a live turn), so this
  // is also where they reappear after being hidden while `running`.
  const wasRunningRef = useRef(false);
  useEffect(() => {
    const settled = wasRunningRef.current && !running;
    wasRunningRef.current = running;
    if (!settled || !suggestSid) return;
    let cancelled = false;
    void (async () => {
      try {
        const r = await api.getSuggestions(suggestSid);
        if (cancelled) return;
        setSuggestions(
          r.excluded === "automation" || !Array.isArray(r.suggestions) ? [] : r.suggestions,
        );
      } catch {
        /* keep whatever is on screen */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [running, suggestSid]);

  // ── V2 P2-1 suggestions: actions + dismissal ───────────────────────────
  /** Dismiss one card for THIS session only; the key includes the session id so
   *  the same suggestion can still appear in another session. */
  function dismissSuggestion(id: string) {
    const sid = session?.id;
    if (!sid) return;
    setSuggDismissed((prev) => {
      const next = { ...prev, [`${sid}:${id}`]: true as const };
      try {
        localStorage.setItem(SUGGEST_DISMISS_LS_KEY, JSON.stringify(next));
      } catch {
        /* in-memory dismissal still applies for this mount */
      }
      return next;
    });
  }

  /** Execute a suggestion's single action. Types mirror suggestions.py. */
  async function runSuggestionAction(s: api.SessionSuggestion) {
    const sid = session?.id;
    if (!sid) return;
    const type = s.action?.type;
    if (type === "open_settings") {
      router.push("/settings/model-api");
      return;
    }
    if (type === "compact") {
      // Reuse the built-in /compact command: async_handler summarizes older
      // messages and rewrites thread state server-side (no graph turn). The
      // server busy-gates it too, but the UI already hides cards while running.
      if (running) {
        showComposerHint(tc("suggest.busyHint"));
        return;
      }
      attemptSend(sid, {
        text: "/compact",
        images: [],
        files: [],
        mentions: [],
        agentId: target ?? session?.agent_id ?? g.agents[0]?.id ?? null,
      });
      return;
    }
    if (type === "switch_model") {
      // The composer's model chip IS the app's model switcher (rendered by this
      // component); opening its menu is the existing flow. The delegation replay
      // view has no composer, so it goes to the settings page instead.
      if (session?.type === "delegation") {
        router.push("/settings/model-api");
        return;
      }
      setModelOpen(true);
      return;
    }
    if (type === "distill") {
      setSuggestBusy(s.id);
      try {
        reportDistill(await api.distillSession(sid, "skill"), "skill");
      } catch {
        pushNotice("error", tc("distill.failed"), { source: "general" });
      } finally {
        setSuggestBusy(null);
      }
    }
  }

  // V2 P2-1: suggestions still visible for this session after local dismissal.
  const visibleSuggestions = session
    ? suggestions.filter((s) => !suggDismissed[`${session.id}:${s.id}`])
    : [];

  return {
    suggestBusy,
    visibleSuggestions,
    dismissSuggestion,
    runSuggestionAction,
  };
}
