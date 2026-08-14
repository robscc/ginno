/**
 * Notification preferences — single source of truth for the desktop
 * notification gate and its sound.
 *
 * Persisted in `~/.ginno/settings.json` (runtime `/api/settings`) under the
 * `"notifications"` key: `{enabled, sound, sound_name}`. A module-level cache
 * fronts the async API because the gate sites (socket completion callbacks in
 * store.tsx / ChatStream.tsx) must read the CURRENT value synchronously —
 * long-lived callbacks capture stale React state, which is why the previous
 * implementation read localStorage inside each callback. The cache is
 * refreshed on app boot (store boot effect) and on every save (Settings →
 * Notifications).
 */

import * as api from "./runtime";

export interface NotifyPrefs {
  /** Master switch (Settings → Notifications → 启用桌面提醒). */
  enabled: boolean;
  /** Whether completion notifications play a macOS system sound. */
  sound: boolean;
  /** macOS system sound name, e.g. "Glass" (/System/Library/Sounds/<name>.aiff). */
  soundName: string;
}

/** Sounds offered in the settings dropdown — all ship with macOS. Keep in
 *  sync with the allow-list in apps/desktop/src/lib.rs. */
export const SOUND_NAMES = ["Glass", "Ping", "Pop", "Funk", "Hero", "Tink"] as const;

export const DEFAULT_PREFS: NotifyPrefs = { enabled: true, sound: true, soundName: "Glass" };

/** Pre-settings.json master switch (localStorage). One-shot migrated. */
const LEGACY_KEY = "ginno-notify";

let prefs: NotifyPrefs = { ...DEFAULT_PREFS };

/** Synchronous read for the gate sites (see module doc). */
export function notifyPrefs(): NotifyPrefs {
  return prefs;
}

/** settings.json wire shape is `{enabled, sound, sound_name}`. Unknown /
 *  malformed fields fall back to defaults rather than breaking the gate. */
function fromWire(raw: unknown): NotifyPrefs {
  const d = { ...DEFAULT_PREFS };
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (typeof o.enabled === "boolean") d.enabled = o.enabled;
    if (typeof o.sound === "boolean") d.sound = o.sound;
    if (
      typeof o.sound_name === "string" &&
      (SOUND_NAMES as readonly string[]).includes(o.sound_name)
    ) {
      d.soundName = o.sound_name;
    }
  }
  return d;
}

function toWire(p: NotifyPrefs): Record<string, unknown> {
  return { enabled: p.enabled, sound: p.sound, sound_name: p.soundName };
}

/**
 * Fetch prefs from the runtime. One-shot migration: the legacy master switch
 * lived in localStorage ("ginno-notify", "0" = off); if settings.json has no
 * "notifications" key yet, carry that choice over, then drop the legacy key.
 * Never throws — on failure (sidecar down) the defaults stay active and the
 * next boot retries.
 */
export async function loadNotifyPrefs(): Promise<NotifyPrefs> {
  try {
    const legacy =
      typeof localStorage !== "undefined" ? localStorage.getItem(LEGACY_KEY) : null;
    const s = (await api.getSettings()) as Record<string, unknown>;
    const existing = s.notifications;
    if (existing === undefined && legacy === "0") {
      prefs = { ...DEFAULT_PREFS, enabled: false };
      s.notifications = toWire(prefs);
      await api.putSettings(s);
    } else {
      prefs = fromWire(existing);
    }
    if (legacy !== null) localStorage.removeItem(LEGACY_KEY);
  } catch {
    /* keep defaults until next boot/save */
  }
  return prefs;
}

/** Persist prefs into settings.json and refresh the sync cache — the cache is
 *  updated only after the write succeeds, so a failed save never lies. */
export async function saveNotifyPrefs(next: Partial<NotifyPrefs>): Promise<NotifyPrefs> {
  const merged = { ...prefs, ...next };
  const s = (await api.getSettings()) as Record<string, unknown>;
  s.notifications = toWire(merged);
  await api.putSettings(s);
  prefs = merged;
  return prefs;
}
