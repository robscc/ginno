/**
 * Floating quick-chat window preferences — the `floating` key of
 * ~/.ginno/settings.json (docs/floating-window-design.md §1.2).
 *
 * Same read-modify-write pattern as lib/notifyPrefs.ts: a module-level cache
 * fronts the async API so the pin window can read `inactive_opacity`
 * synchronously on blur, and every save pushes the Rust-relevant subset to
 * the desktop shell via the `pin_apply_prefs` command (hotkey re-registration,
 * NSWindowCollectionBehavior, pill click-through).
 *
 * The Rust side NEVER writes settings.json (PUT is a full-document overwrite
 * — window geometry lives in ~/.ginno/floating.json instead).
 */

import { invoke } from "@tauri-apps/api/core";
import { isDesktop } from "./desktop";
import * as api from "./runtime";

export interface FloatingPrefs {
  /** global-shortcut crate syntax, e.g. "CommandOrControl+Alt+Space". */
  hotkey: string;
  /** Pin window's initial session mode: own quick session or follow main. */
  defaultMode: "quick" | "follow";
  /** Opacity when unfocused; 1.0 disables dimming (CSS-only, frontend). */
  inactiveOpacity: number;
  visibleOnAllSpaces: boolean;
  /** "avoid" = macOS default; "overlay" = float above fullscreen spaces. */
  fullscreenPolicy: "avoid" | "overlay";
  /** Pill state only: ignore cursor events (pure status light). */
  pillClickThrough: boolean;
  showOnLaunch: boolean;
}

export const DEFAULT_FLOATING: FloatingPrefs = {
  // ⇧⌘Space — must match DEFAULT_HOTKEY in apps/desktop/src/lib.rs.
  // (⌃⌥Space was the old default: it collides with macOS input-source
  // switching, hence the move.)
  hotkey: "CommandOrControl+Shift+Space",
  defaultMode: "quick",
  inactiveOpacity: 0.7,
  visibleOnAllSpaces: true,
  fullscreenPolicy: "avoid",
  pillClickThrough: false,
  showOnLaunch: false,
};

let prefs: FloatingPrefs = { ...DEFAULT_FLOATING };

/** Synchronous read (pin window blur handler, settings form seeding). */
export function pinPrefs(): FloatingPrefs {
  return prefs;
}

/** settings.json wire shape is snake_case; unknown/malformed fields fall
 *  back to defaults rather than breaking the pin window. */
function fromWire(raw: unknown): FloatingPrefs {
  const d = { ...DEFAULT_FLOATING };
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (typeof o.hotkey === "string" && o.hotkey.trim()) d.hotkey = o.hotkey.trim();
    if (o.default_mode === "quick" || o.default_mode === "follow") d.defaultMode = o.default_mode;
    if (typeof o.inactive_opacity === "number" && o.inactive_opacity >= 0.1 && o.inactive_opacity <= 1) {
      d.inactiveOpacity = o.inactive_opacity;
    }
    if (typeof o.visible_on_all_spaces === "boolean") d.visibleOnAllSpaces = o.visible_on_all_spaces;
    if (o.fullscreen_policy === "avoid" || o.fullscreen_policy === "overlay") {
      d.fullscreenPolicy = o.fullscreen_policy;
    }
    if (typeof o.pill_click_through === "boolean") d.pillClickThrough = o.pill_click_through;
    if (typeof o.show_on_launch === "boolean") d.showOnLaunch = o.show_on_launch;
  }
  return d;
}

function toWire(p: FloatingPrefs): Record<string, unknown> {
  return {
    hotkey: p.hotkey,
    default_mode: p.defaultMode,
    inactive_opacity: p.inactiveOpacity,
    visible_on_all_spaces: p.visibleOnAllSpaces,
    fullscreen_policy: p.fullscreenPolicy,
    pill_click_through: p.pillClickThrough,
    show_on_launch: p.showOnLaunch,
  };
}

/** Push the Rust-owned subset to the desktop shell (hotkey / spaces /
 *  fullscreen policy / click-through). Returns whether the current hotkey is
 *  LIVE after the push (false = taken by another app / invalid syntax).
 *  Outside Tauri there is no shell to register anything, so this reports
 *  true (nothing to conflict with) — the hotkey only matters on desktop. */
export async function pushPinPrefsToShell(): Promise<boolean> {
  if (!isDesktop()) return true;
  try {
    return await invoke<boolean>("pin_apply_prefs", { prefs: toWire(prefs) });
  } catch {
    // Shell unavailable (dev browser / older build without the command).
    return true;
  }
}

/** Ask the shell whether the current hotkey actually registered. Used by
 *  Settings → 悬浮窗 to warn about a taken/invalid shortcut on load. */
export async function queryHotkeyActive(): Promise<boolean> {
  if (!isDesktop()) return true;
  try {
    return await invoke<boolean>("pin_hotkey_status");
  } catch {
    return true;
  }
}

/** Fetch prefs from the runtime. Never throws — on failure (sidecar down)
 *  the defaults stay active and the next boot retries. */
export async function loadPinPrefs(): Promise<FloatingPrefs> {
  try {
    const s = (await api.getSettings()) as Record<string, unknown>;
    prefs = fromWire(s.floating);
  } catch {
    /* keep defaults until next boot/save */
  }
  return prefs;
}

export interface SavePinPrefsResult {
  prefs: FloatingPrefs;
  /** false ⇒ the shell could not register the new hotkey (conflict/syntax).
   *  The value is still persisted — the user can retry or pick another. */
  hotkeyOk: boolean;
}

/** Persist prefs into settings.json, refresh the sync cache, then hand the
 *  Rust-relevant subset to the shell. The cache updates only after the write
 *  succeeds, so a failed save never lies. `hotkeyOk` reports whether the
 *  shortcut went live so the UI can warn about conflicts. */
export async function savePinPrefs(
  next: Partial<FloatingPrefs>,
): Promise<SavePinPrefsResult> {
  const merged = { ...prefs, ...next };
  const s = (await api.getSettings()) as Record<string, unknown>;
  s.floating = toWire(merged);
  await api.putSettings(s);
  prefs = merged;
  const hotkeyOk = await pushPinPrefsToShell();
  return { prefs, hotkeyOk };
}

// ── Hotkey recorder helpers (Settings → 悬浮窗) ──────────────────────────
// The recorder captures a keydown and serializes it to the global-shortcut
// crate's accelerator syntax (the same strings Rust parses via
// `"…".parse::<Shortcut>()`), so what the user presses is what registers.

/** Map a KeyboardEvent to an accelerator string, or null if it isn't a
 *  usable combo (bare modifier press, or a key we can't name). */
export function eventToAccelerator(e: KeyboardEvent): string | null {
  // Normalize the physical key from e.code (layout-independent) so e.g. a
  // letter stays a letter regardless of IME state during capture.
  const code = e.code;
  let key: string | null = null;
  if (/^Key[A-Z]$/.test(code)) key = code.slice(3); // KeyG → G
  else if (/^Digit[0-9]$/.test(code)) key = code.slice(5); // Digit1 → 1
  else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) key = code; // F1..F24
  else if (code === "Space") key = "Space";
  else if (code === "Minus") key = "-";
  else if (code === "Equal") key = "=";
  else if (code === "BracketLeft") key = "[";
  else if (code === "BracketRight") key = "]";
  else if (code === "Backslash") key = "\\";
  else if (code === "Semicolon") key = ";";
  else if (code === "Quote") key = "'";
  else if (code === "Comma") key = ",";
  else if (code === "Period") key = ".";
  else if (code === "Slash") key = "/";
  else if (code === "Backquote") key = "`";
  else if (code === "ArrowUp") key = "Up";
  else if (code === "ArrowDown") key = "Down";
  else if (code === "ArrowLeft") key = "Left";
  else if (code === "ArrowRight") key = "Right";
  if (!key) return null; // bare Shift/Alt/Ctrl/Meta press, or unmapped key

  const mods: string[] = [];
  // On macOS the primary modifier is Command; map it to CommandOrControl so
  // the same string is portable (the crate folds it to Super on mac).
  if (e.metaKey) mods.push("CommandOrControl");
  if (e.ctrlKey) mods.push("Control");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");

  // Require at least one modifier, OR a function key (F1..F24 are safe
  // standalone). A bare letter/number would hijack normal typing globally.
  const isFunc = /^F\d+$/.test(key);
  if (mods.length === 0 && !isFunc) return null;

  // Modifier order matters: the crate requires all modifiers before the key.
  // Deterministic order: CommandOrControl, Control, Alt, Shift.
  const finalMods = [
    ...(mods.includes("CommandOrControl") ? ["CommandOrControl"] : []),
    ...(mods.includes("Control") ? ["Control"] : []),
    ...(mods.includes("Alt") ? ["Alt"] : []),
    ...(mods.includes("Shift") ? ["Shift"] : []),
  ];
  return [...finalMods, key].join("+");
}

/** Modifier tokens → mac symbol, in Apple's canonical display order ⌃⌥⇧⌘. */
const MOD_SYMBOL: Array<[string, string]> = [
  ["Control", "⌃"],
  ["Alt", "⌥"],
  ["Shift", "⇧"],
  ["CommandOrControl", "⌘"],
];
const MOD_TOKENS = new Set(MOD_SYMBOL.map(([t]) => t));

/** Non-modifier key tokens → symbol (keys we render as glyphs). */
const KEY_SYMBOL: Record<string, string> = {
  Space: "Space",
  Up: "↑",
  Down: "↓",
  Left: "←",
  Right: "→",
  Enter: "⏎",
  Tab: "⇥",
  Escape: "⎋",
  Delete: "⌫",
};

/** Pretty-print an accelerator for display in Apple's modifier order
 *  (e.g. "CommandOrControl+Shift+Space" → "⇧⌘Space"). Falls back to the
 *  raw token for anything unrecognized. */
export function formatAccelerator(accel: string): string {
  const parts = accel.split("+").filter(Boolean);
  if (parts.length === 0) return accel;
  const present = new Set(parts);
  const mods = MOD_SYMBOL.filter(([t]) => present.has(t)).map(([, s]) => s);
  const keys = parts
    .filter((p) => !MOD_TOKENS.has(p))
    .map((p) => KEY_SYMBOL[p] ?? (p.length === 1 ? p.toUpperCase() : p));
  return [...mods, ...keys].join("");
}
