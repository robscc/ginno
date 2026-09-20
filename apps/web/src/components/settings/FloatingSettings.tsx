"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
            ? "已保存，但该快捷键注册失败——可能被系统或其他应用占用，请换一个"
            : "已保存",
        );
      } catch {
        setPrefs(prev);
        setMsg("保存失败");
      }
    },
    [prefs],
  );

  function toggleNow() {
    if (!isDesktop()) {
      setMsg("仅在桌面应用中可用");
      return;
    }
    invoke("pin_toggle").catch(() => setMsg("悬浮窗不可用（旧版桌面壳？）"));
  }

  if (!prefs) {
    return (
      <div className="px-8 py-7">
        <h2 className="text-lg font-semibold text-txt">悬浮窗</h2>
        <p className="mt-4 text-sm text-faint">加载中…</p>
      </div>
    );
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">悬浮窗</h2>
      <p className="mt-1 text-sm text-muted">
        置顶速聊窗：全局快捷键唤出，胶囊常驻、聊天框速问速答。完整 Agent 能力，权限确认在窗内内联完成。
      </p>
      <div className="mt-4 max-w-md space-y-5">
        {!hotkeyActive && (
          <div className="flex items-start gap-2 rounded-lg border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs leading-relaxed text-yellow">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              当前快捷键（{formatAccelerator(prefs.hotkey)}）未能注册，可能已被系统或其他应用占用。
              菜单栏图标仍可唤出悬浮窗；建议重新录入一个快捷键。
            </span>
          </div>
        )}
        <div>
          <button
            onClick={toggleNow}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:border-violet hover:text-txt"
          >
            显示 / 隐藏悬浮窗
          </button>
          <p className="mt-1 text-xs text-faint">
            等效于菜单栏图标的「显示悬浮窗」，用于立刻预览下面的设置效果。
          </p>
        </div>
        <div>
          <label className="field-label">全局快捷键</label>
          <div className="flex items-center gap-2">
            <HotkeyRecorder
              value={prefs.hotkey}
              onCommit={(hk) => void save({ hotkey: hk })}
            />
            <button
              onClick={() => void save({ hotkey: DEFAULT_FLOATING.hotkey })}
              title={`恢复默认（${formatAccelerator(DEFAULT_FLOATING.hotkey)}）`}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line text-faint transition-colors hover:border-line2 hover:text-txt"
            >
              <RotateCcw className="h-3.5 w-3.5" />
            </button>
          </div>
          <p className="mt-1 text-xs text-faint">
            点输入框后直接按下组合键即可（需包含 ⌘/⌃/⌥ 之一，或单独的 F 功能键）；Esc
            取消。默认 {formatAccelerator(DEFAULT_FLOATING.hotkey)}
            ——旧版的 ⌃⌥Space 会和 macOS「切换输入法」冲突，已弃用。
          </p>
        </div>
        <div>
          <label className="field-label" htmlFor="pin-default-mode">
            默认会话模式
          </label>
          <select
            id="pin-default-mode"
            className="field"
            value={prefs.defaultMode}
            onChange={(e) => void save({ defaultMode: e.target.value as FloatingPrefs["defaultMode"] })}
          >
            <option value="quick">速聊（独立会话）</option>
            <option value="follow">跟随主窗口的当前会话</option>
          </select>
          <p className="mt-1 text-xs text-faint">
            窗口内随时可在标题栏下拉切换；此设置只决定打开时的初始模式。
          </p>
        </div>
        <div>
          <label className="field-label" htmlFor="pin-opacity">
            失焦透明度：{prefs.inactiveOpacity.toFixed(2)}
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
          <p className="mt-1 text-xs text-faint">拉到 1.00 即关闭失焦变暗。</p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.visibleOnAllSpaces}
              onChange={(e) => void save({ visibleOnAllSpaces: e.target.checked })}
            />
            在所有桌面空间显示
          </label>
        </div>
        <div>
          <label className="field-label" htmlFor="pin-fullscreen">
            全屏应用策略
          </label>
          <select
            id="pin-fullscreen"
            className="field"
            value={prefs.fullscreenPolicy}
            onChange={(e) =>
              void save({ fullscreenPolicy: e.target.value as FloatingPrefs["fullscreenPolicy"] })
            }
          >
            <option value="avoid">避让（默认）：不遮挡全屏应用</option>
            <option value="overlay">覆盖：悬浮在全屏应用之上</option>
          </select>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.pillClickThrough}
              onChange={(e) => void save({ pillClickThrough: e.target.checked })}
            />
            胶囊状态穿透点击
          </label>
          <p className="mt-1 text-xs text-faint">
            开启后收起的胶囊变成纯状态灯，鼠标事件穿透到下层应用（无法点击展开，需用快捷键唤出）。
          </p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.showOnLaunch}
              onChange={(e) => void save({ showOnLaunch: e.target.checked })}
            />
            启动时自动显示悬浮窗
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
        <span className="text-xs text-violet">按下新快捷键…（Esc 取消）</span>
      ) : (
        <span className="font-medium tracking-wide">{formatAccelerator(value)}</span>
      )}
    </button>
  );
}
