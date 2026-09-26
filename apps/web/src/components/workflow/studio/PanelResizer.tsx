"use client";

import { useCallback, useEffect, useRef, useState } from "react";

function clamp(v: number, min: number, max: number) {
  return Math.min(max, Math.max(min, v));
}

/**
 * Persisted panel width, namespaced in localStorage (e.g. "ginno:studio-rail-w").
 * Starts at defaultWidth so SSR and the first client render agree — the stored
 * value is read after mount (avoids a hydration mismatch on the inline style)
 * and every write is clamped to [min, max]. When storage is unavailable
 * (private mode, blocked site data) the value simply degrades to in-memory.
 */
export function usePanelWidth(
  storageKey: string,
  defaultWidth: number,
  min: number,
  max: number,
): [number, (v: number | ((prev: number) => number)) => void] {
  const [width, setWidth] = useState(defaultWidth);

  // Read the stored value once it's safe to touch localStorage (client only).
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (raw != null) {
        const n = Number(raw);
        if (Number.isFinite(n)) setWidth(clamp(n, min, max));
      }
    } catch {
      // storage unavailable: keep the default
    }
  }, [storageKey, min, max]);

  // Accepts an updater so drag handlers never apply a delta onto a stale
  // width when two pointermove events land in one batch. The persist inside
  // the updater is idempotent (same key, same value on StrictMode re-runs).
  const set = useCallback(
    (v: number | ((prev: number) => number)) => {
      setWidth((prev) => {
        const next = clamp(typeof v === "function" ? v(prev) : v, min, max);
        try {
          window.localStorage.setItem(storageKey, String(next));
        } catch {
          // private mode / blocked storage: in-memory only
        }
        return next;
      });
    },
    [storageKey, min, max],
  );

  return [width, set];
}

type PanelResizerProps = {
  /** "vertical" = a vertical strip dragged horizontally (adjusts a column). */
  orientation?: "vertical" | "horizontal";
  /** Delta in px since the last move (or the keyboard step, ±16). */
  onDrag: (d: number) => void;
  /** Double-click: restore the panel's default width. */
  onReset?: () => void;
  ariaLabel: string;
  /** Extra classes on the handle root, e.g. "hidden xl:block" so the handle
      follows the same breakpoint that controls its panel's existence. */
  className?: string;
};

/**
 * A thin draggable edge between panels. Transparent at rest, a violet line on
 * hover/drag; pointer-captured so fast drags never escape the handle, and
 * keyboard-adjustable (Arrow keys, ±16px) as a focusable separator.
 */
export function PanelResizer({
  orientation = "vertical",
  onDrag,
  onReset,
  ariaLabel,
  className,
}: PanelResizerProps) {
  const vertical = orientation === "vertical";
  const [dragging, setDragging] = useState(false);
  const active = useRef(false);
  const lastPos = useRef(0);
  const releaseBodyLock = useRef<() => void>(() => {});

  // If the handle unmounts mid-drag (panel hidden by a breakpoint change),
  // still release the body-level select/cursor lock.
  useEffect(() => {
    return () => {
      if (active.current) releaseBodyLock.current();
    };
  }, []);

  const beginDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    // Never let a handle drag reach the canvas / panels underneath.
    e.stopPropagation();
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    active.current = true;
    lastPos.current = vertical ? e.clientX : e.clientY;
    setDragging(true);
    // Lock the page against text selection and stray cursors while dragging;
    // restored (to whatever it was) when the drag ends or the handle unmounts.
    document.body.classList.add("select-none");
    const prevCursor = document.body.style.cursor;
    document.body.style.cursor = vertical ? "col-resize" : "row-resize";
    const restore = () => {
      document.body.classList.remove("select-none");
      document.body.style.cursor = prevCursor;
    };
    releaseBodyLock.current = restore;
  };

  const moveDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!active.current) return;
    const pos = vertical ? e.clientX : e.clientY;
    const d = pos - lastPos.current;
    lastPos.current = pos;
    if (d !== 0) onDrag(d);
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!active.current) return;
    active.current = false;
    setDragging(false);
    releaseBodyLock.current();
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // capture already released (e.g. pointercancel)
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = 16;
    let d = 0;
    if (vertical) {
      if (e.key === "ArrowLeft") d = -step;
      else if (e.key === "ArrowRight") d = step;
    } else {
      if (e.key === "ArrowUp") d = -step;
      else if (e.key === "ArrowDown") d = step;
    }
    if (d !== 0) {
      e.preventDefault();
      onDrag(d);
    }
  };

  return (
    <div
      role="separator"
      aria-orientation={orientation}
      aria-label={ariaLabel}
      tabIndex={0}
      onPointerDown={beginDrag}
      onPointerMove={moveDrag}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={onReset}
      className={`group relative shrink-0 touch-none outline-none focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-violet/60 ${
        vertical ? "w-1 cursor-col-resize" : "h-1 cursor-row-resize"
      } ${dragging ? "bg-violet/10" : ""} ${className ?? ""}`}
    >
      <span
        aria-hidden
        className={`pointer-events-none absolute rounded-full bg-violet/40 transition-opacity ${
          vertical ? "inset-y-1 left-1/2 w-0.5 -translate-x-1/2" : "inset-x-1 top-1/2 h-0.5 -translate-y-1/2"
        } ${dragging ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}
      />
    </div>
  );
}
