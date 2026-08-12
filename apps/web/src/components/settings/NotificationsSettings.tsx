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
      setMsg("已保存");
    } catch {
      setPrefs(prefs);
      setMsg("保存失败");
    }
  }

  function testNotify() {
    if (!prefs) return;
    void notifyNative({
      kind: "test",
      id: "test",
      title: "Ginno 测试通知",
      body: prefs.sound ? `提示音:${prefs.soundName}` : "这是一条测试通知(无声)",
      sound: prefs.sound ? prefs.soundName : undefined,
    }).then((sent) => {
      // Plain-browser dev fallback (WKWebView has no Notification API, so in
      // the packaged app notifyNative is the only path anyway).
      if (sent) return;
      if (typeof Notification === "undefined") {
        setMsg("当前环境无法发送测试通知");
        return;
      }
      try {
        new Notification("Ginno 测试通知", { body: "浏览器通知(开发环境)" });
      } catch {
        setMsg("当前环境无法发送测试通知");
      }
    });
  }

  if (!prefs) {
    return (
      <div className="px-8 py-7">
        <h2 className="text-lg font-semibold text-txt">通知</h2>
        <p className="mt-4 text-sm text-faint">加载中…</p>
      </div>
    );
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">通知</h2>
      <p className="mt-1 text-sm text-muted">
        会话回复完成、Workflow 运行完成时发送系统通知;点击通知跳转到对应内容。仅在你没有查看对应内容时才提醒。
      </p>
      <div className="mt-4 max-w-md space-y-4">
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.enabled}
              onChange={(e) => void save({ enabled: e.target.checked })}
            />
            启用桌面提醒
          </label>
          <p className="mt-1 text-xs text-faint">
            桌面端首次通知时系统会请求通知权限(若被拒绝,需在 系统设置 → 通知 → Ginno 中手动开启)。
          </p>
        </div>
        <div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={prefs.sound}
              onChange={(e) => void save({ sound: e.target.checked })}
            />
            提示音
          </label>
        </div>
        <div>
          <label className="field-label">提示音声音</label>
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
            发送测试通知
          </button>
          <p className="mt-1 text-xs text-faint">
            不受「启用桌面提醒」开关影响,用于验证通知权限和提示音效果。
          </p>
        </div>
        {msg && <div className="text-xs text-muted">{msg}</div>}
      </div>
    </div>
  );
}
