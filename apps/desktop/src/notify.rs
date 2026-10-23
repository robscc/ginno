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

/// Sounds that ship with macOS (`/System/Library/Sounds/<name>.aiff`).
/// Allow-list doubles as sanitization: `sound` arrives over IPC from the
/// webview, so anything unknown is dropped rather than handed to the OS.
/// Keep in sync with SOUND_NAMES in apps/web/src/lib/notifyPrefs.ts.
const SYSTEM_SOUNDS: &[&str] = &[
    "Basso", "Blow", "Bottle", "Frog", "Funk", "Glass", "Hero", "Morse", "Ping", "Sosumi",
    "Submarine", "Purr", "Pop", "Tink",
];

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
    let handle = match n.show() {
        Ok(h) => h,
        Err(e) => {
            // 2026-08-10: notifications silently died here — macOS auth was
            // denied (request_auth_blocking → false) and show()'s error was
            // swallowed by a bare `return`. Never drop the only trace.
            shell_log(&app, &format!("notification show FAILED: {e}"));
            return;
        }
    };
    shell_log(
        &app,
        &format!("notification shown kind={} id={}", payload.kind, payload.id),
    );
    handle.wait_for_action(|action| {
        // "__closed" = dismissed without clicking; anything else = clicked.
        shell_log(
            &app,
            &format!("wait_for_action resolved action={action} kind={} id={}", payload.kind, payload.id),
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
    shell_log(app, &format!("deep_link_open_session sid={sid} token={token}"));
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
