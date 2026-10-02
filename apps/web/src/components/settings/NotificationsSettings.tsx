"use client";

import { useEffect, useState } from "react";
import { notifyNative } from "@/lib/desktop";
import {
  loadNotifyPrefs,
  saveNotifyPrefs,
  SOUND_NAMES,
  type NotifyPrefs,
} from "@/lib/notifyPrefs";

/**
 * Settings → Notifications. Persists to ~/.ginno/settings.json
 * (`notifications` key) via lib/notifyPrefs.ts — same getSettings/putSettings
 * plumbing as the other panels. The test button bypasses the master switch
 * on purpose: it exists to verify permissions/sound while the switch is off.
 */
export function NotificationsSettings() {
  const [prefs, setPrefs] = useState<NotifyPrefs | null>(null);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    void loadNotifyPrefs().then(setPrefs);
  }, []);

  async function save(patch: Partial<NotifyPrefs>) {
    if (!prefs) return;
    // Reflect immediately; saveNotifyPrefs re-syncs the shared cache only
    // after the write lands, so roll back on failure.
    setPrefs({ ...prefs, ...patch });
    setMsg("");
    try {
      const p = await saveNotifyPrefs(patch);
      setPrefs(p);
      setMsg("Saved");
    } catch {
      setPrefs(prefs);
      setMsg("Failed to save");
    }
  }

  function testNotify() {
    if (!prefs) return;
    void notifyNative({
      kind: "test",
      id: "test",
      title: "Ginno Test Notification",
      body: prefs.sound ? `Sound: ${prefs.soundName}` : "This is a test notification (silent)",
      sound: prefs.sound ? prefs.soundName : undefined,
    }).then((sent) => {
      // Plain-browser dev fallback (WKWebView has no Notification API, so in
      // the packaged app notifyNative is the only path anyway).
      if (sent) return;
      if (typeof Notification === "undefined") {
        setMsg("Cannot send a test notification in this environment");
        return;
      }
      try {
        new Notification("Ginno Test Notification", { body: "Browser notification (dev environment)" });
      } catch {
        setMsg("Cannot send a test notification in this environment");
      }
    });
  }

  if (!prefs) {
    return (
      <div className="px-8 py-7">
        <h2 className="text-lg font-semibold text-txt">Notifications</h2>
        <p className="mt-4 text-sm text-faint">Loading…</p>
      </div>
    );
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Notifications</h2>
      <p className="mt-1 text-sm text-muted">
        Send system notifications when a session reply or a Workflow run completes; clicking a
        notification jumps to the corresponding content. You are only notified when you have not
        viewed it yet.
      </p>
      <div className="mt-4 max-w-md space-y-4">
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.enabled}
              onChange={(e) => void save({ enabled: e.target.checked })}
            />
            Enable desktop notifications
          </label>
          <p className="mt-1 text-xs text-faint">
            The system asks for notification permission on the first desktop notification (if
            denied, enable it manually in System Settings → Notifications → Ginno).
          </p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.sound}
              onChange={(e) => void save({ sound: e.target.checked })}
            />
            Sound
          </label>
        </div>
        <div>
          <label className="field-label">Sound name</label>
          <select
            className="field"
            value={prefs.soundName}
            disabled={!prefs.sound}
            onChange={(e) => void save({ soundName: e.target.value })}
          >
            {SOUND_NAMES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div>
          <button
            onClick={testNotify}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:border-violet hover:text-txt"
          >
            Send test notification
          </button>
          <p className="mt-1 text-xs text-faint">
            Not affected by the desktop notification toggle; used to verify notification permission
            and the sound effect.
          </p>
        </div>
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
