"use client";

import { notifyNative } from "../desktop";
import { notifyPrefs } from "../notifyPrefs";
import { uiText } from "../../i18n/uiText";
import { schedText } from "../../i18n/schedText";
import type { ScheduleRun } from "../types";

// ---- Notifications for run completion (P3) ----
// Module-level prev-status map: reloadWorkflowRuns diffs against it to catch
// done/failed transitions while the tab is hidden.
const _prevRunStatus: Record<string, string> = {};

function fmtRunElapsed(r: { started: number; finished?: number | null }): string {
  const s = Math.max(0, Math.round((r.finished ?? Date.now() / 1000) - r.started));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function notifyRunTransitions(
  runs: Array<{ id: string; name?: string; status: string; steps?: Array<{ id: string; title?: string; status: string }>; started: number; finished?: number | null }>,
  onOpenPanel?: () => void,
) {
  // Master gate (Settings → Notifications, persisted in settings.json; the
  // sync cache avoids stale closures — see lib/notifyPrefs.ts). _prevRunStatus
  // keeps updating even while disabled so re-enabling doesn't fire a burst of
  // stale transitions.
  const prefs = notifyPrefs();
  const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
  for (const r of runs) {
    const prev = _prevRunStatus[r.id];
    _prevRunStatus[r.id] = r.status;
    if (prev === undefined || prev === r.status) continue;
    if (!prefs.enabled) continue;
    if (!hidden) continue; // user is looking — the badges already cover it
    let body: string;
    if (r.status === "done") {
      // 通知模板走 ui 域（模块级函数无 hook，uiText 同步读当前 locale）
      body = uiText("notifyRun.completed", { duration: fmtRunElapsed(r) });
    } else if (r.status === "failed") {
      const failed = (r.steps || []).find((s) => s.status === "failed");
      body = failed?.title
        ? uiText("notifyRun.failedAt", { step: failed.title })
        : uiText("notifyRun.failed");
    } else {
      continue;
    }
    const title = uiText(r.status === "done" ? "notifyRun.titleDone" : "notifyRun.titleFailed", {
      name: r.name || uiText("notifyRun.defaultName"),
    });
    // Desktop: the Tauri shell fires a real macOS notification (WKWebView has
    // no window.Notification). Click → window focus + open the Workflow panel.
    void notifyNative({
      kind: "workflow-run",
      id: r.id,
      title,
      body,
      sound: prefs.sound ? prefs.soundName : undefined,
    }).then((sent) => {
      if (sent) return;
      // Plain-browser dev fallback.
      if (typeof Notification === "undefined") return;
      if (Notification.permission === "default") {
        // First time anything runs: ask for permission once (user-initiated
        // work is happening, so the prompt is contextually reasonable).
        try {
          void Notification.requestPermission();
        } catch {
          /* unsupported */
        }
        return;
      }
      if (Notification.permission !== "granted") return;
      try {
        const n = new Notification(title, { body });
        n.onclick = () => {
          window.focus();
          onOpenPanel?.(); // land on the Workflow tab so the run is visible
          n.close();
        };
      } catch {
        /* notification blocked/unsupported */
      }
    });
  }
}

// ---- 定时任务完成通知（schedule-unseen 的桌面提醒面）----
// AppShell 的 /api/ws/schedule 通道收到 run_finished 时调用。与
// notifyRunTransitions 同一闸门（Settings → Notifications 总开关 + 窗口隐藏），
// 但事件驱动、无需状态 diff。两个刻意的排除：
//   · workflow 目标不在这里通知——run 状态轮询的 notifyRunTransitions 已经
//     覆盖（点击直达 workflow 面板），两路都发会连响两条；
//   · skipped_overlap / missed 不发——前者是重叠让路无结果可看，后者在
//     runtime 恢复时可能一次补记一批，会变成通知风暴（红点/徽标已覆盖）。
// 正文优先用 run.summary（runtime 落终态行时已带截断好的结果摘要），错误行
// 用 error 原文，都缺失再退到时长/泛化文案。
export function notifyScheduleRunFinished(
  run: Pick<
    ScheduleRun,
    "run_id" | "task_name" | "target_type" | "status" | "started_at" | "finished_at" | "summary" | "error"
  >,
  devOpen?: (runId: string) => void,
): void {
  const prefs = notifyPrefs();
  if (!prefs.enabled) return;
  if (typeof document !== "undefined" && document.visibilityState === "visible") return;
  if (run.status !== "ok" && run.status !== "error") return;
  if (run.target_type === "workflow") return;
  const ok = run.status === "ok";
  const name = (run.task_name || "").trim() || schedText("notify.defaultName");
  const title = schedText(ok ? "notify.titleDone" : "notify.titleFailed", { name });
  const raw = ok
    ? (run.summary || "").trim() ||
      schedText("notify.completed", {
        duration: fmtRunElapsed({ started: run.started_at ?? 0, finished: run.finished_at }),
      })
    : (run.error || "").trim() || schedText("notify.failed");
  // macOS 横幅只显示头几行——200 字符后省略（runtime 侧已截 160，此为兜底）
  const body = raw.length > 200 ? `${raw.slice(0, 199)}…` : raw;
  void notifyNative({
    kind: "schedule-run",
    id: run.run_id,
    title,
    body,
    sound: prefs.sound ? prefs.soundName : undefined,
  }).then((sent) => {
    if (sent) return;
    // Plain-browser dev fallback — WKWebView has no Notification API, so
    // inside the packaged app this branch is a silent no-op.
    if (typeof Notification === "undefined") return;
    if (Notification.permission === "default") {
      try {
        void Notification.requestPermission();
      } catch {
        /* unsupported */
      }
      return;
    }
    if (Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, { body });
      n.onclick = () => {
        window.focus();
        devOpen?.(run.run_id);
        n.close();
      };
    } catch {
      /* notification blocked/unsupported */
    }
  });
}
