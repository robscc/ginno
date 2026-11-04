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
  /** V2: per-kind switches — each background event class can be muted
   *  independently (master `enabled` still gates everything). Defaults ON. */
  kinds: NotifyKinds;
  /** V2: notification body privacy — `full` (default) shows the reply
   *  summary, `titleOnly` shows just the session/event title (screen-share /
   *  lock-screen caution). */
  privacy: NotifyPrivacy;
  /** V2: do-not-disturb window — inside the window no banners/sounds are
   *  fired (events still land in Notification Center). Default off. */
  dnd: NotifyDnd;
}

/** Notification event classes. `reply` covers plain turn completion (V1
 *  behaviour); the rest are the V2 trigger-surface extensions. */
export type NotifyKind = "reply" | "askUser" | "failure" | "workflow" | "subagent";

export type NotifyKinds = Record<NotifyKind, boolean>;

export type NotifyPrivacy = "full" | "titleOnly";

export interface NotifyDnd {
  enabled: boolean;
  /** Inclusive window bounds, "HH:MM" 24h; may wrap midnight ("22:00"→"08:00"). */
  from: string;
  to: string;
}

/** Sounds offered in the settings dropdown — all ship with macOS. Keep in
 *  sync with the allow-list in apps/desktop/src/notify.rs. */
export const SOUND_NAMES = ["Glass", "Ping", "Pop", "Funk", "Hero", "Tink"] as const;

/** Failure notifications always use Basso (macOS failure cue) regardless of
 *  the completion sound — one glance/earful tells done from broken. */
export const FAILURE_SOUND = "Basso";

export const KINDS: readonly NotifyKind[] = ["reply", "askUser", "failure", "workflow", "subagent"];

export const DEFAULT_PREFS: NotifyPrefs = {
  enabled: true,
  sound: true,
  soundName: "Glass",
  kinds: { reply: true, askUser: true, failure: true, workflow: true, subagent: true },
  privacy: "full",
  dnd: { enabled: false, from: "22:00", to: "08:00" },
};

/** Pre-settings.json master switch (localStorage). One-shot migrated. */
const LEGACY_KEY = "ginno-notify";

let prefs: NotifyPrefs = { ...DEFAULT_PREFS };

/** Synchronous read for the gate sites (see module doc). */
export function notifyPrefs(): NotifyPrefs {
  return prefs;
}

/** settings.json wire shape is `{enabled, sound, sound_name, kinds, privacy,
 *  dnd}`. Unknown / malformed fields fall back to defaults rather than
 *  breaking the gate; pre-V2 files (first three keys only) migrate with all
 *  kinds on, full privacy, DnD off. Exported for unit tests. */
export function fromWire(raw: unknown): NotifyPrefs {
  const d = { ...DEFAULT_PREFS, kinds: { ...DEFAULT_PREFS.kinds } };
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
    if (o.kinds && typeof o.kinds === "object") {
      for (const k of KINDS) {
        const v = (o.kinds as Record<string, unknown>)[k];
        if (typeof v === "boolean") d.kinds[k] = v;
      }
    }
    if (o.privacy === "titleOnly") d.privacy = "titleOnly";
    if (o.dnd && typeof o.dnd === "object") {
      const w = o.dnd as Record<string, unknown>;
      if (typeof w.enabled === "boolean") d.dnd.enabled = w.enabled;
      if (typeof w.from === "string" && /^\d{2}:\d{2}$/.test(w.from)) d.dnd.from = w.from;
      if (typeof w.to === "string" && /^\d{2}:\d{2}$/.test(w.to)) d.dnd.to = w.to;
    }
  }
  return d;
}

function toWire(p: NotifyPrefs): Record<string, unknown> {
  return {
    enabled: p.enabled,
    sound: p.sound,
    sound_name: p.soundName,
    kinds: { ...p.kinds },
    privacy: p.privacy,
    dnd: { ...p.dnd },
  };
}

/** True when `at` (minutes since midnight) falls inside the DnD window.
 *  Windows may wrap midnight: from 22:00 → to 08:00 covers 23:59 and 07:00. */
export function inDndWindow(dnd: NotifyDnd, atMin: number): boolean {
  if (!dnd.enabled) return false;
  const toMin = (s: string) => {
    const [h, m] = s.split(":").map(Number);
    return h * 60 + m;
  };
  const f = toMin(dnd.from);
  const t = toMin(dnd.to);
  if (Number.isNaN(f) || Number.isNaN(t)) return false;
  return f <= t ? atMin >= f && atMin < t : atMin >= f || atMin < t;
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
