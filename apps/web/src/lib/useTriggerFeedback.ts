"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Shared reaction state machine for workflow trigger buttons (work item D).
 *
 * Buttons used to only swap their label ("运行" → "运行中…") and swallow HTTP
 * failures silently. This hook gives every trigger a consistent lifecycle:
 * idle → busy → success (brief green flash) | error (shake + inline message).
 * Callers keep owning the actual async work; they just report the outcome.
 */
export type TriggerPhase = "idle" | "busy" | "success" | "error";

export interface TriggerFeedback {
  phase: TriggerPhase;
  /** Error message to render inline (only meaningful when phase === "error"). */
  message: string | null;
  /** Call right before starting the async trigger. */
  start: () => void;
  /** Call when the trigger succeeded (run created / retry accepted). */
  succeed: () => void;
  /** Call when the trigger failed; `msg` is shown inline. */
  fail: (msg?: string) => void;
  /** Animation class for the button element ("" | anim-shake | anim-success-flash). */
  animClass: string;
}

export function useTriggerFeedback(opts?: {
  successHoldMs?: number;
  errorHoldMs?: number;
}): TriggerFeedback {
  const successHold = opts?.successHoldMs ?? 900;
  const errorHold = opts?.errorHoldMs ?? 2500;
  const [phase, setPhase] = useState<TriggerPhase>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  };
  useEffect(() => clearTimer, []);

  const start = useCallback(() => {
    clearTimer();
    setMessage(null);
    setPhase("busy");
  }, []);

  const succeed = useCallback(() => {
    clearTimer();
    setPhase("success");
    setMessage(null);
    timer.current = setTimeout(() => setPhase("idle"), successHold);
  }, [successHold]);

  const fail = useCallback(
    (msg?: string) => {
      clearTimer();
      setPhase("error");
      setMessage(msg || "触发失败");
      // Hold the error long enough to read, then let the button re-arm. The
      // message stays visible until the next attempt clears it via start().
      timer.current = setTimeout(() => setPhase("idle"), errorHold);
    },
    [errorHold],
  );

  const animClass =
    phase === "error" ? "anim-shake" : phase === "success" ? "anim-success-flash" : "";

  return { phase, message, start, succeed, fail, animClass };
}
