"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
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
  const t = useTranslations("settings.notifications");
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
      setMsg(t("saved"));
    } catch {
      setPrefs(prefs);
      setMsg(t("saveFailed"));
    }
  }

  function testNotify() {
    if (!prefs) return;
    void notifyNative({
      kind: "test",
      id: "test",
      title: t("testTitle"),
      body: prefs.sound ? t("testBodySound", { sound: prefs.soundName }) : t("testBodySilent"),
      sound: prefs.sound ? prefs.soundName : undefined,
    }).then((sent) => {
      // Plain-browser dev fallback (WKWebView has no Notification API, so in
      // the packaged app notifyNative is the only path anyway).
      if (sent) return;
      if (typeof Notification === "undefined") {
        setMsg(t("testUnavailable"));
        return;
      }
      try {
        new Notification(t("testTitle"), { body: t("testBodyBrowser") });
      } catch {
        setMsg(t("testUnavailable"));
      }
    });
  }

  if (!prefs) {
    return (
      <div className="px-8 py-7">
        <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
        <p className="mt-4 text-sm text-faint">{t("loading")}</p>
      </div>
    );
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 text-sm text-muted">{t("description")}</p>
      <div className="mt-4 max-w-md space-y-4">
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.enabled}
              onChange={(e) => void save({ enabled: e.target.checked })}
            />
            {t("enableLabel")}
          </label>
          <p className="mt-1 text-xs text-faint">{t("enableHelp")}</p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.sound}
              onChange={(e) => void save({ sound: e.target.checked })}
            />
            {t("soundLabel")}
          </label>
        </div>
        <div>
          <label className="field-label">{t("soundNameLabel")}</label>
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
            {t("testButton")}
          </button>
          <p className="mt-1 text-xs text-faint">{t("testHelp")}</p>
        </div>
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
