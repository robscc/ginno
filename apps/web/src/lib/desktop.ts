/**
 * Tauri shell detection + native bridges.
 *
 * Detection is dependency-free; the only behavioral forks today are file
 * saving (WKWebView can't trigger browser downloads — anchor[download] on a
 * blob URL is a no-op — so the desktop UI asks the sidecar to copy the file
 * into ~/Downloads instead, see runtime.saveFileToDownloads) and native
 * notifications (WKWebView has no window.Notification — the shell fires real
 * macOS notifications via tauri-plugin-notification, see notifyNative below).
 */

import { emit } from "@tauri-apps/api/event";
import {
  applyPrivacy,
  notifyPrefs,
  osGateOpen,
  soundFor,
  type NotifyKind,
} from "./notifyPrefs";

export function isDesktop(): boolean {
  if (typeof window === "undefined") return false;
  if ("__TAURI_INTERNALS__" in window) return true;
  // Release-build fallback: the packaged webview may arrive at this origin via
  // the splash page's location.replace (see apps/desktop/src/lib.rs), in which
  // case the Tauri bridge may not be present. Detect WKWebView directly — its
  // user agent contains AppleWebKit but neither a Safari nor a Chrome token.
  const ua = navigator.userAgent;
  return ua.includes("AppleWebKit") && !ua.includes("Safari") && !ua.includes("Chrome");
}

/** Payload for the shell-side native notification (apps/desktop/src/lib.rs). */
export interface NativeNotification {
  /** Click target kind: focus a chat session, open the Workflow panel, open a
   *  scheduled-run replay, open the background-tasks page, or "test" (Settings
   *  → Notifications test button) which only refocuses.
   *
   *  ⚠️ 这是**点击目标**，不是通知分类。两者正交：`kind:"session"` 的通知可以
   *  属于 reply / askUser / failure 任一分类。历史上只有这一个 `kind` 字段，
   *  导致 NoticeHost 的失焦兜底被迫借用 `kind:"test"`（点击只能 refocus）来
   *  发一条 failure 类通知。分类走下面的 `notifyKind`。 */
  kind: "session" | "workflow-run" | "schedule-run" | "bg-task" | "test";
  /** 通知分类（闸门轴）——**必填**，让 TS 逼每个调用点声明自己属于哪一类，
   *  从而不可能漏过 enabled / kinds / DnD / privacy 判定。
   *  `"test"` 是设置页测试按钮专用，刻意绕过偏好闸门（见 osGateOpen）。 */
  notifyKind: NotifyKind | "test";
  /** Session id (kind=session), run id (kind=workflow-run), scheduled run id
   *  (kind=schedule-run), or task id (kind=bg-task). */
  id: string;
  title: string;
  /** 正文。`privacy: "titleOnly"` 时由 notifyNative 统一抹空——调用点不需要
   *  自己裁（此前只有 4/7 个站点裁了，其余把全文塞进横幅）。 */
  body: string;
  /** macOS system sound name ("Glass", "Ping", …); omitted = derive from
   *  `notifyKind` + prefs (failure → Basso, 其余 → 用户选的提示音, sound 关 →
   *  静音). The shell validates against its own allow-list. */
  sound?: string;
}

/**
 * Is this window allowed to own OS notifications?
 *
 * Only the main window is. The floating quick-chat window (`/pin`) mounts
 * AppShell for its hooks, and every hook runs BEFORE AppShell's
 * `if (pathname === "/pin") return <>{children}</>` early-exit (AppShell.tsx:56
 * `useNoticeNativeFallback`, :522 schedule WS → `notifyScheduleRunFinished`,
 * store.tsx:309 `useWorkflows` polling → `notifyRunTransitions`). The pin
 * window is created hidden and kept alive hidden (pin.rs `ensure_pin_window`),
 * so its `document.visibilityState !== "visible"` is ALWAYS true — the "only
 * notify when the user isn't looking" premise those three sites gate on can
 * never fail there. Result: scheduled/workflow completions fired a banner even
 * while the user stared at the main window, and fired TWICE whenever the main
 * window was hidden as well (per-window module state, no cross-window dedupe).
 *
 * Gating here (the single funnel every OS notification passes through) covers
 * all six call sites at once, including ones added later.
 * See docs/notifications-audit-2026-11-14.md P0-3.
 */
export function isNotifyOwnerWindow(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.location.pathname !== "/pin";
  } catch {
    return true;
  }
}

// 同一个调度 run 的完成与摘要补写都经过这里。记录随 webview 存活，
// 与用于点击回跳、会淘汰旧行的缓存独立。
const scheduledRunNotifications = new Set<string>();

/**
 * Fire a native macOS notification via the Tauri shell.
 *
 * **所有 OS 通知的唯一出口**，因此偏好闸门装在这里而不是散在各调用点。修复前
 * 7 个发通知的站点各自拼闸门，覆盖极不均匀（审计文档 P1-1 的矩阵）：
 * `handle.ts` 的 lastwords 与 `NoticeHost` 的失焦兜底**完全不过**总开关/分类/
 * 勿扰/隐私，`store/notifications.ts` 的三个函数只过总开关——于是「关掉桌面
 * 提醒」关不干净、「开了勿扰」照样弹、「仅标题」隐私对一半站点无效。
 *
 * 出口统一判定：桌面环境 → 通知所有者窗口 → osGateOpen(notifyKind)
 * （总开关 ∧ 分类开关 ∧ 不在勿扰窗）→ 隐私裁正文 → 推导提示音 → emit。
 * 调用点自己的 `shouldNotify` 与此幂等重叠，是有意的双保险。
 *
 * Returns true for a successful handoff or a duplicate scheduled run with a
 * handoff in progress or already completed. Returns false outside Tauri,
 * when this window is not the notification owner, when the
 * prefs gate is closed, or when the bridge is unavailable (e.g. the splash-page
 * location.replace path) — callers may fall back to the HTML5 Notification API,
 * which covers plain-browser dev (it is a no-op inside WKWebView anyway).
 *
 * ⚠️ 返回 false 现在**同时**表示「被偏好闸门拦下」，不再只是「桥不可用」。
 * 浏览器兜底分支因此会在通知被用户关掉时仍然弹 HTML5 通知——各调用点的兜底
 * 需要自己再判一次 osGateOpen，见 store/notifications.ts。
 */
export async function notifyNative(n: NativeNotification): Promise<boolean> {
  if (!isDesktop()) return false;
  if (!isNotifyOwnerWindow()) return false;
  if (!osGateOpen(n.notifyKind)) return false;
  const np = notifyPrefs();
  const body = n.notifyKind === "test" ? n.body : applyPrivacy(n.body, np);
  const sound =
    n.sound ?? (n.notifyKind === "test" ? undefined : soundFor(n.notifyKind));
  const scheduled = n.kind === "schedule-run";
  if (scheduled) {
    if (scheduledRunNotifications.has(n.id)) return true;
    // emit 尚未返回时也拦住并发补写帧。
    scheduledRunNotifications.add(n.id);
  }
  try {
    await emit("ginno:notify", {
      kind: n.kind,
      // 壳侧只用于日志（notify.rs shell_log），serde 忽略未知字段，旧壳兼容
      notify_kind: n.notifyKind,
      id: n.id,
      title: n.title,
      body,
      sound,
    });
    return true;
  } catch {
    if (scheduled) scheduledRunNotifications.delete(n.id);
    return false;
  }
}

/**
 * Ask the Tauri shell to hold / release a `caffeinate -i` child
 * (scheduled-tasks-design.md §3.3). Only blocks idle system sleep — the screen
 * may still dim/lock; lid close and manual sleep win regardless.
 *
 * Returns true when the command reached the shell. Not desktop (dev in a plain
 * browser) or the bridge is unavailable → false; callers degrade gracefully
 * (the runtime's keep_awake flag stays the source of truth either way).
 */
export async function setKeepAwake(enabled: boolean): Promise<boolean> {
  if (!isDesktop()) return false;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_keep_awake", { enabled });
    return true;
  } catch {
    return false;
  }
}

/**
 * Login-item autostart status — SMAppService registration is the ONLY source
 * of truth (reboot-resume foundation): no local boolean is ever cached, every
 * call re-reads the registration state, so drift caused by the user toggling
 * Ginno off under System Settings → General → Login Items is reflected here
 * faithfully. Returns null when not in the Tauri shell (browser dev) — the
 * settings toggle hides itself in that case.
 */
export async function getAutostartEnabled(): Promise<boolean | null> {
  if (!isDesktop()) return null;
  try {
    // 2026-11-22 巡检 P1：插件 isEnabled 只查 LaunchAgent plist 文件存在
    // （系统设置「允许在后台」禁用后仍恒 true，UI 恒显已开启）。改走自有
    // command：SMAppService 注册态（系统登录项真源）|| legacy plist 存在
    //（老版本注册的、自启真实在跑就显示开）。
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<boolean>("ginno_autostart_is_enabled");
  } catch {
    return null;
  }
}

/**
 * Register/unregister the login item. 2026-11-22 巡检 P1 修正：release 走
 * SMAppService（系统登录项真源，可在 系统设置 → 通用 → 登录项 管理），
 * dev/未打包/旧系统回退 LaunchAgent；老版本遗留的 LaunchAgent plist 在
 * enable 时迁移清理、disable 时一并删除。On failure returns the message;
 * on success callers should re-read getAutostartEnabled() to confirm the
 * system-side state rather than trusting a local flag.
 */
export async function setAutostart(
  enabled: boolean,
): Promise<{ ok: boolean; error?: string }> {
  if (!isDesktop()) return { ok: false, error: "not desktop" };
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("ginno_autostart_set", { enabled });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
