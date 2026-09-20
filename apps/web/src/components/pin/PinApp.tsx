"use client";

/**
 * PinApp — root of the floating quick-chat window (/pin route).
 * Docs: docs/floating-window-design.md §2–§3.
 *
 * Two shapes, one webview:
 *   • pill — 148×44 status light (Rust owns the resize); click = expand,
 *     drag (>4px threshold) = move via startDragging().
 *   • mini — 360×520 chat card: header (session-mode dropdown + actions)
 *     and PinStream. PinStream stays MOUNTED (display:none) while pill'd so
 *     the status dot and the turn keep flowing.
 *
 * State ownership split (§2.1): Rust owns window mode + geometry; this
 * component only mirrors the mode via `pin_get_mode` / the `pin:mode` event
 * and asks for changes via `pin_set_mode` / `pin_hide` / `pin_open_main`.
 *
 * Session modes (§1.1 Q2 — default independent, switchable to follow):
 *   • quick  — the window's own throwaway session (type:"quick", ⚡ in the
 *     main sidebar), persisted across launches via localStorage.
 *   • follow — mirrors the main window's ACTIVE session through a
 *     BroadcastChannel; the runtime broadcasts every turn to all sockets of
 *     a session, so both windows see (and may answer) the same stream.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { ChevronDown, ExternalLink, Loader2, Plus, X } from "lucide-react";
import { LAST_SESSION_KEY, useGinno } from "@/lib/store";
import { isDesktop } from "@/lib/desktop";
import { loadPinPrefs, pinPrefs } from "@/lib/pinPrefs";
import * as api from "@/lib/runtime";
import { PinStream, type PinStreamStatus } from "@/components/pin/PinStream";

type WinMode = "pill" | "mini";
type SessMode = "quick" | "follow";

// §2.3: pin-owned localStorage keys all use the ginno-pin-* prefix (the pin
// window NEVER writes ginno-last-session — that belongs to the main window).
const QUICK_KEY = "ginno-pin-session";
const SESS_MODE_KEY = "ginno-pin-session-mode";

export function PinApp() {
  const g = useGinno();

  const [winMode, setWinMode] = useState<WinMode>("mini");
  const [sessMode, setSessMode] = useState<SessMode>(() => {
    try {
      const v = localStorage.getItem(SESS_MODE_KEY);
      if (v === "quick" || v === "follow") return v;
    } catch {
      /* SSR / storage blocked */
    }
    return "quick";
  });
  const [quickId, setQuickId] = useState<string | null>(null);
  const [followedId, setFollowedId] = useState<string | null>(null);
  const [status, setStatus] = useState<PinStreamStatus>("idle");
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [dimmed, setDimmed] = useState(false);

  const initRef = useRef(false);
  const creatingRef = useRef(false);
  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  // The Tauri webview is transparent; strip the app's opaque page ground.
  useEffect(() => {
    document.documentElement.classList.add("pin-window");
    return () => document.documentElement.classList.remove("pin-window");
  }, []);

  // Prefs: default session mode (only when the user never picked one) and
  // inactive-dimming opacity. loadPinPrefs never throws.
  useEffect(() => {
    loadPinPrefs().then((p) => {
      let stored: string | null = null;
      try {
        stored = localStorage.getItem(SESS_MODE_KEY);
      } catch {
        /* ignore */
      }
      if (stored !== "quick" && stored !== "follow") setSessMode(p.defaultMode);
    });
  }, []);

  // Mirror the Rust-owned window mode: initial value + push updates
  // (tray menu / global hotkey / Esc-from-native all change it there).
  useEffect(() => {
    if (!isDesktop()) return;
    let unlisten: (() => void) | null = null;
    let disposed = false;
    invoke<string>("pin_get_mode")
      .then((m) => setWinMode(m === "pill" ? "pill" : "mini"))
      .catch(() => {
        /* older shell without pin commands — stay mini */
      });
    listen<{ mode?: string }>("pin:mode", (e) => {
      const m = e.payload?.mode;
      if (m === "pill" || m === "mini") setWinMode(m);
    })
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {
        /* events unavailable outside Tauri */
      });
    // Dim when unfocused (§1.2 inactive_opacity; 1.0 = disabled).
    const win = getCurrentWindow();
    let unFocus: (() => void) | null = null;
    win
      .onFocusChanged(({ payload }) => setDimmed(!payload))
      .then((fn) => {
        if (disposed) fn();
        else unFocus = fn;
      })
      .catch(() => {
        /* ignore */
      });
    return () => {
      disposed = true;
      unlisten?.();
      unFocus?.();
    };
  }, []);

  // Follow mode: subscribe to the main window's active-session broadcast.
  // Seed from the shared last-session key (same-origin WKWebViews share
  // localStorage, so this survives a pin relaunch with no main window up).
  useEffect(() => {
    try {
      const seed = localStorage.getItem(LAST_SESSION_KEY);
      if (seed) setFollowedId(seed);
    } catch {
      /* ignore */
    }
    let bc: BroadcastChannel | null = null;
    try {
      bc = new BroadcastChannel("ginno-active-session");
      bc.onmessage = (e) => {
        const d = e.data as { type?: string; sessionId?: string | null } | null;
        if (d?.type === "active") setFollowedId(typeof d.sessionId === "string" ? d.sessionId : null);
      };
    } catch {
      /* BroadcastChannel unavailable — follow mode falls back to the seed */
    }
    return () => bc?.close();
  }, []);

  const createQuickSession = useCallback(async () => {
    if (creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setSessionError(null);
    try {
      const res = await api.createSession({
        workspace: process.env.NEXT_PUBLIC_WORKSPACE ?? "/tmp/gw",
        type: "quick",
        title: "速聊",
      });
      if (!res?.id) throw new Error(res?.error || "创建速聊会话失败");
      setQuickId(res.id);
      try {
        localStorage.setItem(QUICK_KEY, res.id);
      } catch {
        /* ignore */
      }
      await g.reloadSessions();
    } catch (e) {
      setSessionError(e instanceof Error ? e.message : String(e));
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [g.reloadSessions]);

  // One-shot quick-session init once the store boots: reuse the persisted
  // id when it still exists, else mint a fresh quick session.
  useEffect(() => {
    if (!g.ready || initRef.current) return;
    initRef.current = true;
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(QUICK_KEY);
    } catch {
      /* ignore */
    }
    if (stored && g.sessions.some((s) => s.id === stored)) {
      setQuickId(stored);
      return;
    }
    if (stored) {
      try {
        localStorage.removeItem(QUICK_KEY);
      } catch {
        /* ignore */
      }
    }
    void createQuickSession();
  }, [g.ready, g.sessions, createQuickSession]);

  const activeId = sessMode === "follow" ? followedId : quickId;
  const followedTitle =
    g.sessions.find((s) => s.id === followedId)?.title ?? null;

  const setMode = useCallback((m: WinMode) => {
    setWinMode(m); // optimistic: Rust echoes pin:mode back anyway
    if (isDesktop()) {
      invoke("pin_set_mode", { mode: m }).catch(() => {
        // Re-sync from the shell if the command failed.
        invoke<string>("pin_get_mode")
          .then((v) => setWinMode(v === "pill" ? "pill" : "mini"))
          .catch(() => {
            /* ignore */
          });
      });
    }
  }, []);

  const hide = useCallback(() => {
    if (isDesktop()) invoke("pin_hide").catch(() => { /* ignore */ });
  }, []);

  const openMain = useCallback(() => {
    if (!isDesktop()) {
      window.location.href = "/";
      return;
    }
    invoke("pin_open_main", { sessionId: activeId ?? null }).catch(() => {
      /* ignore */
    });
  }, [activeId]);

  const switchSessMode = useCallback((m: SessMode) => {
    setSessMode(m);
    try {
      localStorage.setItem(SESS_MODE_KEY, m);
    } catch {
      /* ignore */
    }
  }, []);

  const newQuickSession = useCallback(() => {
    switchSessMode("quick");
    void createQuickSession();
  }, [switchSessMode, createQuickSession]);

  // Window-level shortcuts: Esc → collapse to pill, ⌘↵ → hand off to main.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        setMode("pill");
        return;
      }
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        openMain();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setMode, openMain]);

  // Pill click-vs-drag (§2.2): data-tauri-drag-region would swallow the
  // click, so drag manually — record mousedown, hand off to startDragging()
  // past a 4px threshold, treat a plain mouseup as "expand".
  useEffect(() => {
    function onMove(e: MouseEvent) {
      const d = dragRef.current;
      if (!d || d.moved) return;
      if (Math.abs(e.clientX - d.x) > 4 || Math.abs(e.clientY - d.y) > 4) {
        d.moved = true;
        if (isDesktop()) getCurrentWindow().startDragging().catch(() => { /* ignore */ });
      }
    }
    function onUp() {
      const d = dragRef.current;
      dragRef.current = null;
      if (d && !d.moved) setMode("mini");
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [setMode]);

  const opacity = dimmed ? pinPrefs().inactiveOpacity : 1;

  // ── pill shape ──────────────────────────────────────────────────────
  if (winMode === "pill") {
    return (
      <div
        className="flex h-screen w-screen cursor-pointer select-none items-center justify-center transition-opacity duration-200"
        style={{ opacity }}
      >
        <div
          onMouseDown={(e) => {
            if (e.button === 0) dragRef.current = { x: e.clientX, y: e.clientY, moved: false };
          }}
          title="Ginno 速聊 — 点击展开，拖动移动"
          className="flex h-full w-full items-center gap-2 rounded-full border border-line2 bg-panel/90 px-3.5 shadow-lg backdrop-blur-md"
        >
          {/* key={status} re-triggers the one-shot bounce on → permission */}
          <span
            key={status}
            className={
              "h-2 w-2 shrink-0 rounded-full " +
              (status === "permission"
                ? "bg-yellow pin-bounce"
                : status === "busy"
                  ? "animate-pulse bg-green"
                  : "bg-faint")
            }
          />
          <span className="truncate text-xs font-medium text-muted">
            {status === "permission" ? "待确认" : status === "busy" ? "运行中" : "Ginno"}
          </span>
        </div>
        {/* Stream stays mounted while pill'd — status dot + running turns */}
        <div className="hidden">
          {activeId && <PinStream key={activeId} sessionId={activeId} onStatus={setStatus} />}
        </div>
      </div>
    );
  }

  // ── mini shape ──────────────────────────────────────────────────────
  return (
    <div
      className="flex h-screen w-screen flex-col overflow-hidden rounded-xl border border-line2 bg-panel/95 shadow-2xl backdrop-blur-md transition-opacity duration-200"
      style={{ opacity }}
    >
      {/* header: drag region + session mode + actions */}
      <div
        data-tauri-drag-region
        className="flex h-9 shrink-0 items-center gap-1.5 border-b border-line px-2"
      >
        <select
          value={sessMode}
          onChange={(e) => switchSessMode(e.target.value as SessMode)}
          title="会话模式"
          className="min-w-0 max-w-[170px] flex-1 cursor-pointer truncate rounded-md border border-transparent bg-transparent px-1 py-0.5 text-xs font-medium text-muted outline-none transition-colors hover:border-line2 hover:text-txt focus:border-violet/60"
        >
          <option value="quick">⚡ 速聊（独立会话）</option>
          <option value="follow">
            {followedTitle ? `👁 跟随 · ${followedTitle}` : "👁 跟随主窗口会话"}
          </option>
        </select>
        <div className="ml-auto flex shrink-0 items-center">
          <button
            type="button"
            onClick={newQuickSession}
            title="新建速聊会话"
            className="rounded-md p-1 text-faint transition-colors hover:bg-card hover:text-txt"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={openMain}
            title="在主窗口打开（⌘↵）"
            className="rounded-md p-1 text-faint transition-colors hover:bg-card hover:text-txt"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setMode("pill")}
            title="收为胶囊（Esc）"
            className="rounded-md p-1 text-faint transition-colors hover:bg-card hover:text-txt"
          >
            <ChevronDown className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={hide}
            title="隐藏悬浮窗"
            className="rounded-md p-1 text-faint transition-colors hover:bg-card hover:text-red"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* body */}
      {activeId ? (
        <PinStream key={activeId} sessionId={activeId} onStatus={setStatus} />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
          {!g.ready ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin text-faint" />
              <p className="text-xs text-faint">等待 Ginno 启动…</p>
            </>
          ) : sessionError ? (
            <>
              <p className="text-xs leading-relaxed text-red">{sessionError}</p>
              <button
                type="button"
                onClick={() => void createQuickSession()}
                className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-muted transition-colors hover:text-txt"
              >
                重试
              </button>
            </>
          ) : creating ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin text-faint" />
              <p className="text-xs text-faint">正在创建速聊会话…</p>
            </>
          ) : (
            <>
              <p className="text-xs leading-relaxed text-faint">
                主窗口暂无活动会话。
                <br />
                切回「⚡ 速聊」，或先去主窗口打开一个会话。
              </p>
              <button
                type="button"
                onClick={() => switchSessMode("quick")}
                className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-muted transition-colors hover:text-txt"
              >
                切回速聊
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
