//! Native notifications: the web UI emits `ginno:notify`; this module shows
//! the macOS banner and, on click, restores the main window and navigates the
//! webview to the notification's target.

use tauri::Manager;

use crate::common::shell_log;

/// Payload of the `ginno:notify` event emitted by the web UI (see
/// `notifyNative` in apps/web/src/lib/desktop.ts) when a session turn,
/// workflow run, or scheduled-task run finishes while the user isn't looking
/// at it.
#[derive(serde::Deserialize)]
pub(crate) struct NotifyPayload {
    /// `"session"`, `"workflow-run"`, `"schedule-run"` — decides which bridge
    /// global is called on click — or `"test"` (Settings → Notifications test
    /// button), which only refocuses the window.
    pub(crate) kind: String,
    /// 通知**分类**（reply / askUser / failure / workflow / schedule / bgTask /
    /// test）——与上面的 `kind`（点击目标）正交：`kind:"session"` 的通知可以
    /// 属于 reply、askUser 或 failure 任一分类。
    ///
    /// 只用于日志。此前 shell.log 只记点击目标，2026-11-14 排查通知风暴时
    /// 无法从日志区分「3858 条 session 通知」里哪些是 reply、哪些是误发的
    /// askUser，只能靠排除法推断（审计文档 P2 可观测性）。web 侧从
    /// `notifyNative` 唯一出口带上；旧 web bundle 不发这个字段，故 Option。
    #[serde(default)]
    pub(crate) notify_kind: Option<String>,
    /// Session id (`kind == "session"`), run id (`kind == "workflow-run"`), or
    /// scheduled run id (`kind == "schedule-run"`).
    pub(crate) id: String,
    pub(crate) title: String,
    pub(crate) body: String,
    /// macOS system sound name ("Glass", "Ping", …); absent = silent. The web
    /// UI reads the preference from settings.json (`notifications.sound` /
    /// `sound_name`) and passes it per-event, so the shell stays stateless.
    #[serde(default)]
    pub(crate) sound: Option<String>,
}

impl NotifyPayload {
    /// 日志上下文：点击目标 + 通知分类 + 标题前缀。
    ///
    /// 加**分类**是因为 2026-11-14 排查通知风暴时，shell.log 里 3858 条
    /// `kind=session` 无法区分是 reply 还是误发的 askUser，只能靠排除法推断
    /// （审计文档 P2「可观测性不足」）。加**标题前缀**是为了在同分类内进一步
    /// 定位：「⏸」= ask_user 挂起、「⚠️」= 失败、「📡」= 订阅会话完成。
    /// 标题截 40 字符——它来自会话名，可能很长，行内还要留给 id 与错误文本。
    pub(crate) fn log_ctx(&self) -> String {
        let nk = self.notify_kind.as_deref().unwrap_or("?");
        // chars() 而非字节切片：标题是中文/emoji，按字节切会截出非法 UTF-8
        let short: String = self.title.chars().take(40).collect();
        format!(
            " kind={} notify_kind={nk} id={} title={short:?}",
            self.kind, self.id
        )
    }
}

/// Sounds that ship with macOS (`/System/Library/Sounds/<name>.aiff`).
/// Allow-list doubles as sanitization: `sound` arrives over IPC from the
/// webview, so anything unknown is dropped rather than handed to the OS.
/// Keep in sync with SOUND_NAMES in apps/web/src/lib/notifyPrefs.ts.
const SYSTEM_SOUNDS: &[&str] = &[
    "Basso",
    "Blow",
    "Bottle",
    "Frog",
    "Funk",
    "Glass",
    "Hero",
    "Morse",
    "Ping",
    "Sosumi",
    "Submarine",
    "Purr",
    "Pop",
    "Tink",
];

/// 并发等待上限：notify-rust 的 UN 后端靠轮询已投递列表检测点击/关闭，
/// 每个未决通知一个轮询线程——通知风暴时几十路并发轮询曾把 ginno-desktop
/// 打到 58% CPU、系统 usernoted 打到 84%（2026-11-10 实测，3s sample 里
/// 159 帧在 getDeliveredNotifications）。超过上限的新通知 fire-and-forget：
/// 保底 4 路点击跳转，更早的通知点击后仅弹窗不导航（信息也早已过时）。
const MAX_ACTION_WAITERS: usize = 4;
static ACTIVE_WAITERS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// Fire a native macOS notification and wait for the user's reaction.
///
/// Uses notify-rust's NSUserNotification path directly: Tauri's notification
/// plugin drops the handle on desktop and offers no click callback, while
/// notify-rust's `wait_for_action` resolves with the interaction (delegate
/// callbacks arrive on the main run loop, which the Tauri event loop keeps
/// pumping). Each notification parks its own thread — notifications are rare
/// and the cost is negligible; an uninteracted notification keeps its thread
/// until clicked from Notification Center (macOS keeps alerts there
/// indefinitely and a late click must still navigate).
pub(crate) fn show_notification_and_wait(app: tauri::AppHandle, payload: NotifyPayload) {
    let mut n = notify_rust::Notification::new();
    n.summary(&payload.title).body(&payload.body);
    // Sound preference arrives per-event (settings.json → web UI → payload).
    // The UN backend maps this to UNNotificationSound soundNamed:, resolving
    // against /System/Library/Sounds; unknown names are dropped here.
    if let Some(name) = payload.sound.as_deref() {
        if SYSTEM_SOUNDS.contains(&name) {
            n.sound_name(name);
        } else {
            shell_log(&app, &format!("ignoring unknown sound name {name:?}"));
        }
    }
    // 有界重试投递（2026-11-14，docs/notifications-audit-2026-11-14.md P0-2）。
    //
    // `show()` 在 UN 后端要过 mac-usernotifications 的 `block_on_current`：不在
    // 主线程时，它要求 `CFRunLoop::main().is_waiting()` 为真才肯发送，否则直接
    // 返回 `MainThreadNotRunning`。而 `is_waiting()` 只在主 run loop **睡在等待
    // 相位**时为真——主线程正在执行回调（例如正在派发触发本通知的那个
    // `ginno:notify` 事件）时它是 false。这是个**假阴性**：run loop 明明在跑、
    // 响应迟早会送达，库却拒发。
    //
    // 实测：shell.log 里 2357 次 `show FAILED: Mainthread not running`，占投递
    // 尝试的 40%；失败率随通知密度变化——低负载区 0-5%，风暴区 30-53%。是负载
    // 相关的竞态，不是「线程用错了」。
    //
    // 所以这里做有界重试，而**不是**把 show() 搬到 `run_on_main_thread`：搬过去
    // 会让紧随其后的 `wait_for_action`（等用户点击，可能几分钟）也跑在主线程上，
    // 直接冻住 UI。重试只是等主线程派发完、回到等待相位。
    //
    // 重试不会重复投递：`block_on_current` 在 poll future **之前**就做该判定，
    // 返回 Err 时通知根本没发出去。
    const SHOW_ATTEMPTS: u32 = 5;
    let mut attempt = 0u32;
    let handle = loop {
        attempt += 1;
        match n.show() {
            Ok(h) => break h,
            Err(e) if attempt < SHOW_ATTEMPTS => {
                shell_log(
                    &app,
                    &format!(
                        "notification show retry {attempt}/{SHOW_ATTEMPTS}: {e}{}",
                        payload.log_ctx()
                    ),
                );
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
            Err(e) => {
                // 2026-08-10: notifications silently died here — macOS auth was
                // denied (request_auth_blocking → false) and show()'s error was
                // swallowed by a bare `return`. Never drop the only trace.
                shell_log(
                    &app,
                    &format!(
                        "notification show FAILED after {attempt} attempts: {e}{}",
                        payload.log_ctx()
                    ),
                );
                return;
            }
        }
    };
    shell_log(&app, &format!("notification shown{}", payload.log_ctx()));
    let in_flight = ACTIVE_WAITERS.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
    if in_flight > MAX_ACTION_WAITERS {
        // 超限：不再排队等点击（避免轮询线程无界增长），立即归还名额。
        ACTIVE_WAITERS.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
        shell_log(
            &app,
            &format!(
                "notification fire-and-forget ({} waiters in flight){}",
                in_flight,
                payload.log_ctx()
            ),
        );
        return;
    }
    handle.wait_for_action(|action| {
        ACTIVE_WAITERS.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
        // "__closed" = dismissed without clicking; anything else = clicked.
        shell_log(
            &app,
            &format!(
                "wait_for_action resolved action={action}{}",
                payload.log_ctx()
            ),
        );
        if action != "__closed" {
            focus_and_open(&app, &payload.kind, &payload.id);
        }
    });
}

/// Restore the window and tell the webview to navigate to the notification's
/// target. Same eval convention as the DragDrop → `__ginnoFileDrop` bridge;
/// the globals are registered in AppShell and survive because close = hide
/// (the webview stays alive). The target id is JSON-escaped — never
/// interpolated raw.
pub(crate) fn focus_and_open(app: &tauri::AppHandle, kind: &str, id: &str) {
    shell_log(app, &format!("focus_and_open kind={kind} id={id}"));
    // "test" (Settings → Notifications test button): refocus only — there is
    // no target to navigate to.
    let script = if kind == "test" {
        String::new()
    } else if kind == "workflow-run" {
        "window.__ginnoOpenWorkflowRun && window.__ginnoOpenWorkflowRun();".to_string()
    } else if kind == "schedule-run" {
        // AppShell registers __ginnoOpenScheduleRun(runId): opens the shadow-
        // session replay for prompt targets, falls back to the /scheduled
        // runs list when the row cache is gone (webview reloaded).
        let id_js = serde_json::to_string(id).unwrap_or_else(|_| "\"\"".to_string());
        format!("window.__ginnoOpenScheduleRun && window.__ginnoOpenScheduleRun({id_js});")
    } else if kind == "bg-task" {
        // Background-task completion (background-tasks-design.md §6.3):
        // AppShell registers __ginnoOpenTasks() → navigates to /tasks. The
        // task id is not navigable on its own (no per-task route).
        "window.__ginnoOpenTasks && window.__ginnoOpenTasks();".to_string()
    } else if kind == "tasks-overview" {
        // Running-tasks overview panel (tasks-overview §2.2 entry): AppShell
        // registers __ginnoOpenTasksOverview() → workspace + 「运行」tab.
        "window.__ginnoOpenTasksOverview && window.__ginnoOpenTasksOverview();".to_string()
    } else {
        let id_js = serde_json::to_string(id).unwrap_or_else(|_| "\"\"".to_string());
        format!("window.__ginnoOpenSession && window.__ginnoOpenSession({id_js});")
    };
    let app = app.clone();
    let inner = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(w) = inner.get_webview_window("main") {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
            crate::common::sync_dock_policy(&inner);
            let _ = w.eval(&script);
        }
    });
}

/// Monotonic token of the newest deep-link navigation. A cold-boot retry loop
/// (below) checks it every second and dies when a NEWER deep link takes over —
/// without this, two overlapping retry loops would ping-pong the webview
/// between their sessions until both expired.
static DEEP_LINK_TOKEN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Handle a `ginno://session/<id>` deep link (Vibe Island card click — the
/// jumpRule in vibe_island.py's manifest; macOS delivers the URL through
/// RunEvent::Opened in lib.rs).
///
/// Warm start (window already on the app page): the immediate focus + eval
/// lands right away. Cold start (macOS launched us FROM the click): the
/// webview sits on the boot splash for seconds and `__ginnoOpenSession` is
/// not registered yet — the guarded eval no-ops, so retry once a second.
/// The JS-side marker (`__ginnoDeepLinkDone === token`) gives once-per-call
/// semantics: this call's retries navigate exactly once (so a late retry
/// never yanks the user back after they manually switched away), while a
/// NEW click — same card or a different one — carries a fresh token and
/// navigates again.
pub(crate) fn deep_link_open_session(app: &tauri::AppHandle, sid: &str) {
    use std::sync::atomic::Ordering;

    let token = DEEP_LINK_TOKEN.fetch_add(1, Ordering::SeqCst) + 1;
    shell_log(
        app,
        &format!("deep_link_open_session sid={sid} token={token}"),
    );
    let id_js = serde_json::to_string(sid).unwrap_or_else(|_| "\"\"".to_string());
    let script = format!(
        "if (window.__ginnoOpenSession && window.__ginnoDeepLinkDone !== {token}) {{ \
         window.__ginnoDeepLinkDone = {token}; window.__ginnoOpenSession({id_js}); }}"
    );
    let h = app.clone();
    let s = script.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(w) = h.get_webview_window("main") {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
            crate::common::sync_dock_policy(&h);
            let _ = w.eval(&s);
        }
    });
    // Cold-boot retry window: the sidecar boot budget is 60s (shell_ui), so
    // 45×1s covers a cold launch with margin. Cheap: one guarded eval a
    // second, and the token check kills the loop the moment a newer deep
    // link (or a warm click that already landed) supersedes it.
    let h = app.clone();
    std::thread::spawn(move || {
        for _ in 0..45 {
            std::thread::sleep(std::time::Duration::from_secs(1));
            if DEEP_LINK_TOKEN.load(Ordering::SeqCst) != token {
                break; // a newer deep link owns the navigation now
            }
            let h2 = h.clone();
            let s2 = script.clone();
            let _ = h.run_on_main_thread(move || {
                if let Some(w) = h2.get_webview_window("main") {
                    let _ = w.eval(&s2);
                }
            });
        }
    });
}
