"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, Keyboard, RotateCcw } from "lucide-react";
import { invoke } from "@tauri-apps/api/core";
import { isDesktop } from "@/lib/desktop";
import {
  DEFAULT_FLOATING,
  eventToAccelerator,
  formatAccelerator,
  loadPinPrefs,
  queryHotkeyActive,
  savePinPrefs,
  type FloatingPrefs,
} from "@/lib/pinPrefs";

/**
 * Settings → 悬浮窗. Persists the `floating` key of ~/.ginno/settings.json
 * via lib/pinPrefs.ts (same read-modify-write pattern as Notifications);
 * every save pushes the Rust-relevant subset (hotkey / spaces / fullscreen
 * policy / click-through) to the desktop shell via `pin_apply_prefs`, which
 * reports back whether the hotkey actually registered so we can warn about
 * conflicts (⌃⌥Space etc. colliding with macOS input-source switching).
 */
export function FloatingSettings() {
  const t = useTranslations("settings.floating");
  const [prefs, setPrefs] = useState<FloatingPrefs | null>(null);
  const [hotkeyActive, setHotkeyActive] = useState(true);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    void loadPinPrefs().then((p) => {
      setPrefs(p);
      // Reflect whether the shell currently has a LIVE registration.
      void queryHotkeyActive().then(setHotkeyActive);
    });
  }, []);

  const save = useCallback(
    async (patch: Partial<FloatingPrefs>) => {
      if (!prefs) return;
      const prev = prefs;
      setPrefs({ ...prefs, ...patch });
      setMsg("");
      try {
        const { prefs: p, hotkeyOk } = await savePinPrefs(patch);
        setPrefs(p);
        if ("hotkey" in patch) setHotkeyActive(hotkeyOk);
        setMsg(
          "hotkey" in patch && !hotkeyOk
            ? t("savedHotkeyConflict")
            : t("saved"),
        );
      } catch {
        setPrefs(prev);
        setMsg(t("saveFailed"));
      }
    },
    [prefs, t],
  );

  function toggleNow() {
    if (!isDesktop()) {
      setMsg(t("desktopOnly"));
      return;
    }
    invoke("pin_toggle").catch(() => setMsg(t("unavailable")));
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
      <div className="mt-4 max-w-md space-y-5">
        {!hotkeyActive && (
          <div className="flex items-start gap-2 rounded-lg border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs leading-relaxed text-yellow">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{t("hotkeyWarn", { hotkey: formatAccelerator(prefs.hotkey) })}</span>
          </div>
        )}
        <div>
          <button
            onClick={toggleNow}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:border-violet hover:text-txt"
          >
            {t("toggleButton")}
          </button>
          <p className="mt-1 text-xs text-faint">{t("toggleHelp")}</p>
        </div>
        <div>
          <label className="field-label">{t("hotkeyLabel")}</label>
          <div className="flex items-center gap-2">
            <HotkeyRecorder
              value={prefs.hotkey}
              onCommit={(hk) => void save({ hotkey: hk })}
            />
            <button
              onClick={() => void save({ hotkey: DEFAULT_FLOATING.hotkey })}
              title={t("resetTitle", { hotkey: formatAccelerator(DEFAULT_FLOATING.hotkey) })}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line text-faint transition-colors hover:border-line2 hover:text-txt"
            >
              <RotateCcw className="h-3.5 w-3.5" />
            </button>
          </div>
          <p className="mt-1 text-xs text-faint">
            {t("hotkeyHelp", { hotkey: formatAccelerator(DEFAULT_FLOATING.hotkey) })}
          </p>
        </div>
        <div>
          <label className="field-label" htmlFor="pin-default-mode">
            {t("defaultModeLabel")}
          </label>
          <select
            id="pin-default-mode"
            className="field"
            value={prefs.defaultMode}
            onChange={(e) => void save({ defaultMode: e.target.value as FloatingPrefs["defaultMode"] })}
          >
            <option value="quick">{t("modeQuick")}</option>
            <option value="follow">{t("modeFollow")}</option>
          </select>
          <p className="mt-1 text-xs text-faint">{t("defaultModeHelp")}</p>
        </div>
        <div>
          <label className="field-label" htmlFor="pin-opacity">
            {t("opacityLabel", { value: prefs.inactiveOpacity.toFixed(2) })}
          </label>
          <input
            id="pin-opacity"
            type="range"
            min={0.1}
            max={1}
            step={0.05}
            value={prefs.inactiveOpacity}
            onChange={(e) => void save({ inactiveOpacity: Number(e.target.value) })}
            className="w-full accent-violet"
          />
          <p className="mt-1 text-xs text-faint">{t("opacityHelp")}</p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.visibleOnAllSpaces}
              onChange={(e) => void save({ visibleOnAllSpaces: e.target.checked })}
            />
            {t("spacesLabel")}
          </label>
        </div>
        <div>
          <label className="field-label" htmlFor="pin-fullscreen">
            {t("fullscreenLabel")}
          </label>
          <select
            id="pin-fullscreen"
            className="field"
            value={prefs.fullscreenPolicy}
            onChange={(e) =>
              void save({ fullscreenPolicy: e.target.value as FloatingPrefs["fullscreenPolicy"] })
            }
          >
            <option value="avoid">{t("fsAvoid")}</option>
            <option value="overlay">{t("fsOverlay")}</option>
          </select>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.pillClickThrough}
              onChange={(e) => void save({ pillClickThrough: e.target.checked })}
            />
            {t("clickThroughLabel")}
          </label>
          <p className="mt-1 text-xs text-faint">{t("clickThroughHelp")}</p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.showOnLaunch}
              onChange={(e) => void save({ showOnLaunch: e.target.checked })}
            />
            {t("showOnLaunchLabel")}
          </label>
        </div>
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}

/**
 * Click-to-record hotkey input. Shows the current accelerator as mac
 * symbols; while recording, the next valid keydown becomes the new value
 * (Esc cancels). Invalid captures (bare modifier, modifier-less letter) are
 * ignored so the field never swallows a half-pressed combo.
 */
function HotkeyRecorder({
  value,
  onCommit,
}: {
  value: string;
  onCommit: (accel: string) => void;
}) {
  const t = useTranslations("settings.floating");
  const [recording, setRecording] = useState(false);
  const btnRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!recording) return;
    function onKey(e: KeyboardEvent) {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") {
        setRecording(false);
        return;
      }
      const accel = eventToAccelerator(e);
      if (!accel) return; // bare modifier or unsupported key — keep waiting
      setRecording(false);
      if (accel !== value) onCommit(accel);
    }
    // Capture phase so the recorder wins over any other app shortcuts.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [recording, value, onCommit]);

  useEffect(() => {
    if (recording) btnRef.current?.focus();
  }, [recording]);

  return (
    <button
      ref={btnRef}
      type="button"
      onClick={() => setRecording((r) => !r)}
      onBlur={() => setRecording(false)}
      className={
        "flex h-9 min-w-[140px] flex-1 items-center justify-center gap-2 rounded-lg border px-3 text-sm transition-colors " +
        (recording
          ? "border-violet bg-violet/10 text-txt"
          : "border-line bg-base/60 text-txt hover:border-line2")
      }
    >
      <Keyboard className="h-3.5 w-3.5 shrink-0 text-faint" />
      {recording ? (
        <span className="text-xs text-violet">{t("recording")}</span>
      ) : (
        <span className="font-medium tracking-wide">{formatAccelerator(value)}</span>
      )}
    </button>
  );
}
