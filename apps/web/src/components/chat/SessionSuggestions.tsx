"use client";

// SessionSuggestions — V2 P2-1 execution-time suggestion cards.
//
// Rendered above the composer by ChatStream (which owns fetching, per-session
// localStorage dismissal and the actions themselves). This component is
// presentational: one compact card per suggestion, severity colour, exactly one
// action button plus a dismiss ✕. The backend never emits tips without an
// action (suggestions.py), and automation sessions are filtered server-side.

import { Loader2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { SessionSuggestion } from "@/lib/runtime";

/** high = red, medium = yellow, low = faint (see P2-1 spec). */
const TONE: Record<
  SessionSuggestion["severity"],
  { card: string; dot: string; title: string }
> = {
  high: {
    card: "border-red/40 bg-red/[0.06]",
    dot: "bg-red",
    title: "text-red",
  },
  medium: {
    card: "border-yellow/40 bg-yellow/[0.06]",
    dot: "bg-yellow",
    title: "text-yellow",
  },
  low: {
    card: "border-line bg-card/50",
    dot: "bg-line2",
    title: "text-faint",
  },
};

export function SessionSuggestions({
  suggestions,
  busyId,
  onAction,
  onDismiss,
}: {
  suggestions: SessionSuggestion[];
  /** Suggestion id whose action is in flight — disables every button. */
  busyId: string | null;
  onAction: (s: SessionSuggestion) => void;
  onDismiss: (id: string) => void;
}) {
  const tc = useTranslations("chat");
  if (suggestions.length === 0) return null;

  const actionLabel = (type: string): string | null => {
    switch (type) {
      case "open_settings":
        return tc("suggest.actions.openSettings");
      case "compact":
        return tc("suggest.actions.compact");
      case "switch_model":
        return tc("suggest.actions.switchModel");
      case "distill":
        return tc("suggest.actions.distill");
      default:
        return null;
    }
  };

  return (
    <div className="mb-2 flex flex-col gap-1.5" role="list" aria-label={tc("suggest.heading")}>
      {suggestions.map((s) => {
        const tone = TONE[s.severity] ?? TONE.low;
        const label = actionLabel(s.action?.type ?? "");
        const busy = busyId === s.id;
        return (
          <div
            key={s.id}
            role="listitem"
            className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs ${tone.card}`}
          >
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden />
            <div className="min-w-0 flex-1">
              <div className={`truncate font-medium ${tone.title}`} title={s.title}>
                {s.title}
              </div>
              <div className="truncate text-[11px] text-muted" title={s.detail}>
                {s.detail}
              </div>
            </div>
            {label && (
              <button
                onClick={() => onAction(s)}
                disabled={busyId !== null}
                className="flex shrink-0 items-center gap-1 rounded-md border border-line2 bg-card px-2 py-0.5 text-[11px] text-txt transition-colors hover:border-line hover:bg-card2 disabled:opacity-50"
              >
                {busy && <Loader2 className="h-3 w-3 animate-spin" />}
                {label}
              </button>
            )}
            <button
              onClick={() => onDismiss(s.id)}
              title={tc("suggest.dismiss")}
              aria-label={tc("suggest.dismiss")}
              className="shrink-0 rounded p-0.5 text-faint transition-colors hover:text-txt"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
