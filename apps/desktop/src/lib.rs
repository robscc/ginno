//! Tauri shell for Ginno.
//!
//! Responsibilities:
//!   1. Spawn the bundled Python runtime (`resources/runtime/ginno-runtime`,
//!      a PyInstaller onedir bundle) in release builds.
//!   2. Keep the window responsive while the runtime boots: if its HTTP server
//!      isn't reachable within a short grace period, the webview shows a local
//!      splash page (data: URL — needs no server) that polls `/api/health` and
//!      navigates to the app the moment the runtime is up.
//!   3. Forward runtime stdout/stderr to a log file under ~/.ginno/logs/.
//!   4. Terminate the runtime on app exit.
//!   5. Native notifications: the web UI emits `ginno:notify` when a session
//!      turn / workflow run finishes while the user looks away; the shell
//!      shows the macOS notification and, on click, restores the window and
//!      tells the webview to navigate to the target (`__ginnoOpenSession` /
//!      `__ginnoOpenWorkflowRun`, same eval convention as `__ginnoFileDrop`).
//!      Closing the window hides it (macOS convention) so the webview and its
//!      sockets survive to keep receiving completion events.
//!
//! In dev (`tauri dev`), the user runs `pnpm dev:runtime` in a separate
//! terminal; this file only spawns the runtime in release builds.

use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{DragDropEvent, Listener, Manager, WindowEvent};

const SIDECAR_PORT: u16 = 8787;

/// Handle to the spawned runtime process, so `RunEvent::Exit` can terminate it.
struct RuntimeProcess(Mutex<Option<Child>>);

/// Payload of the `ginno:notify` event emitted by the web UI (see
/// `notifyNative` in apps/web/src/lib/desktop.ts) when a session turn or
/// workflow run finishes while the user isn't looking at it.
#[derive(serde::Deserialize)]
struct NotifyPayload {
    /// `"session"` or `"workflow-run"` — decides which bridge global is called.
    kind: String,
    /// Session id (`kind == "session"`) or run id (`kind == "workflow-run"`).
    id: String,
    title: String,
    body: String,
}

fn open_log_file(app: &tauri::App) -> Option<std::fs::File> {
    let home = dirs_home(app);
    let logs = home.join("logs");
    std::fs::create_dir_all(&logs).ok()?;
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("sidecar.log"))
        .ok()
}

fn dirs_home(app: &tauri::App) -> std::path::PathBuf {
    // Honor $GINNO_HOME for tests, otherwise ~/.ginno.
    if let Ok(p) = std::env::var("GINNO_HOME") {
        return std::path::PathBuf::from(p);
    }
    let home = app
        .path()
        .home_dir()
        .expect("home dir");
    home.join(".ginno")
}

/// Append one line to ~/.ginno/logs/shell.log (same convention as sidecar.log).
/// Best-effort diagnostics for the notification / window-visibility flow.
fn shell_log<M: tauri::Manager<tauri::Wry>>(app: &M, line: &str) {
    let home = if let Ok(p) = std::env::var("GINNO_HOME") {
        std::path::PathBuf::from(p)
    } else {
        match app.path().home_dir() {
            Ok(h) => h.join(".ginno"),
            Err(_) => return,
        }
    };
    let logs = home.join("logs");
    let _ = std::fs::create_dir_all(&logs);
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(logs.join("shell.log"))
    {
        use std::io::Write;
        let _ = writeln!(f, "{line}");
    }
}

/// Reclaim the runtime port from a stale `ginno-runtime`, if one holds it.
///
/// The packaged runtime is rebuilt *in place*: if a previous app instance's
/// runtime is still alive when a new build replaces its files, the old process
/// keeps the port but can no longer load anything from the replaced bundle,
/// surfacing as broken chat turns. Such a process is unrecoverable; kill it
/// (and only it — verified by process name) so the fresh runtime can bind.
#[cfg(not(debug_assertions))]
fn kill_stale_sidecar() {
    use std::process::Command;

    let addr: SocketAddr = ([127, 0, 0, 1], SIDECAR_PORT).into();
    if TcpStream::connect_timeout(&addr, Duration::from_millis(150)).is_err() {
        return; // port already free
    }
    let Ok(list) = Command::new("lsof")
        .args(["-t", &format!("-iTCP:{SIDECAR_PORT}"), "-sTCP:LISTEN"])
        .output()
    else {
        return;
    };
    for pid in String::from_utf8_lossy(&list.stdout).split_whitespace() {
        // Never kill a stranger on the port — only a ginno-runtime of ours.
        let is_ours = Command::new("ps")
            .args(["-p", pid, "-o", "comm="])
            .output()
            .map(|o| String::from_utf8_lossy(&o.stdout).contains("ginno-runtime"))
            .unwrap_or(false);
        if is_ours {
            let _ = Command::new("kill").arg(pid).status();
        }
    }
    // Wait for the port to free up; escalate to SIGKILL once if it lingers.
    let mut escalated = false;
    for _ in 0..30 {
        std::thread::sleep(Duration::from_millis(100));
        if TcpStream::connect_timeout(&addr, Duration::from_millis(100)).is_err() {
            return;
        }
        if !escalated {
            escalated = true;
            if let Ok(list) = Command::new("lsof")
                .args(["-t", &format!("-iTCP:{SIDECAR_PORT}"), "-sTCP:LISTEN"])
                .output()
            {
                for pid in String::from_utf8_lossy(&list.stdout).split_whitespace() {
                    let _ = Command::new("kill").arg("-9").arg(pid).status();
                }
            }
        }
    }
}

#[cfg(not(debug_assertions))]
const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Minimal base64 encoder (no extra dependency needed for one data: URL).
#[cfg(not(debug_assertions))]
fn base64(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64_ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(B64_ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64_ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64_ALPHABET[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

/// Self-contained loading page shown while the runtime boots.
///
/// Served from a data: URL so it needs no server; it polls the runtime's
/// /api/health and hands over to the app (same-origin with the API) once the
/// runtime is reachable. `__PORT__` is substituted at runtime.
#[cfg(not(debug_assertions))]
const SPLASH_HTML: &str = r#"<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { height: 100%; margin: 0; }
  body {
    background: #0b0d12; color: #e6e8ee;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .box { text-align: center; }
  .spin {
    width: 34px; height: 34px; margin: 0 auto 18px; border-radius: 50%;
    border: 3px solid rgba(255,255,255,.14); border-top-color: #7aa2ff;
    animation: r .9s linear infinite;
  }
  @keyframes r { to { transform: rotate(360deg); } }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 10px; letter-spacing: .5px; }
  p { font-size: 13px; color: #9aa0ad; margin: 0; min-height: 18px; }
</style>
</head>
<body>
<div class="box">
  <div class="spin"></div>
  <h1>Ginno</h1>
  <p id="s">正在启动运行时…</p>
</div>
<script>
  /* Static splash: no network calls (a data: page fetching loopback would be
     blocked by Private Network Access in some engines). The Tauri shell polls
     the runtime port and navigates this webview to the app once it is up; the
     script below only animates a status line while we wait. */
  var t0 = Date.now();
  function status(msg) { var el = document.getElementById('s'); if (el) el.textContent = msg; }
  function tick() {
    var s = Math.round((Date.now() - t0) / 1000);
    if (s > 120) {
      status('启动时间较长（' + s + 's），仍在等待… 如持续失败请重启应用');
    } else if (s > 8) {
      status('首次启动需要一点时间，正在加载依赖…（' + s + 's）');
    } else {
      status('正在启动运行时…（' + s + 's）');
    }
    setTimeout(tick, 500);
  }
  tick();
</script>
</body>
</html>"#;

/// Error page shown when the runtime never came up within the 60s budget.
///
/// Same data:-URL constraints as SPLASH_HTML (no network JS, no Tauri IPC):
/// the 重试 button is a plain top-level navigation (PNA never gates those),
/// and a background poller in the shell auto-navigates to the app once the
/// port accepts — so recovery works even without clicking.
#[cfg(not(debug_assertions))]
const ERROR_HTML: &str = r#"<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { height: 100%; margin: 0; }
  body {
    background: #0a0a0f; color: #e9e9f0;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .box { text-align: center; width: min(420px, 90vw); }
  .ic {
    width: 48px; height: 48px; margin: 0 auto 16px; border-radius: 14px;
    background: rgba(239,68,68,.12); color: #ef4444;
    display: flex; align-items: center; justify-content: center;
  }
  h1 { font-size: 16px; font-weight: 600; margin: 0 0 8px; }
  p { font-size: 12.5px; color: #9a9aa6; margin: 0; }
  code {
    display: inline-block; margin-top: 10px; font: 11px/1.6 ui-monospace, monospace;
    color: #9a9aa6; background: #15151d; border: 1px solid #262632;
    border-radius: 6px; padding: 4px 8px;
  }
  .row { margin-top: 20px; }
  button {
    background: #8b5cf6; color: #fff; border: none; border-radius: 9px;
    padding: 8px 18px; font-size: 13px; cursor: pointer;
  }
  button:hover { filter: brightness(1.12); }
  .note { margin-top: 14px; font-size: 11px; color: #62626e; }
</style>
</head>
<body>
<div class="box">
  <div class="ic">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.3 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.7 3.86a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
  </div>
  <h1>运行时启动失败</h1>
  <p>等待 sidecar 就绪超时（60s）。会话数据不受影响。</p>
  <code>~/.ginno/logs/sidecar.log</code>
  <div class="row">
    <button onclick="location.href='http://127.0.0.1:__PORT__/'">重试</button>
  </div>
  <div class="note">运行时就绪后将自动进入应用</div>
</div>
</body>
</html>"#;

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
fn show_notification_and_wait(app: tauri::AppHandle, payload: NotifyPayload) {
    let mut n = notify_rust::Notification::new();
    n.summary(&payload.title).body(&payload.body);
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
fn focus_and_open(app: &tauri::AppHandle, kind: &str, id: &str) {
    shell_log(app, &format!("focus_and_open kind={kind} id={id}"));
    let script = if kind == "workflow-run" {
        "window.__ginnoOpenWorkflowRun && window.__ginnoOpenWorkflowRun();".to_string()
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

pub fn run() {
    // Set while a real quit is in flight (⌘Q / menu / ExitRequested) so the
    // CloseRequested handler below destroys the window instead of hiding it.
    let quitting = Arc::new(AtomicBool::new(false));

    let app = tauri::Builder::default()
        // WKWebView never fires the HTML5 `ondrop` for files dragged from the
        // Finder, so the composer's JS drop handler can't see them. Handle the
        // OS-level drop natively and forward the file paths to the page via
        // `window.__ginnoFileDrop` (defined in ChatStream), which attaches them
        // through the runtime's /api/files/attach-path endpoint.
        .on_window_event({
            let quitting = quitting.clone();
            move |window, event| {
                match event {
                    WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                        if paths.is_empty() {
                            return;
                        }
                        if let Some(webview) = window.get_webview_window("main") {
                            let paths_json = serde_json::to_string(paths)
                                .unwrap_or_else(|_| "[]".to_string());
                            let _ = webview.eval(&format!(
                                "window.__ginnoFileDrop && window.__ginnoFileDrop({paths_json});"
                            ));
                        }
                    }
                    // macOS convention: closing the window hides it instead of
                    // destroying it, so the webview and its per-session sockets
                    // stay alive and background turn completions can still fire
                    // notifications. Real quit is ⌘Q / menu Quit — ExitRequested
                    // flips the flag first, making this a real close; the
                    // sidecar is terminated on RunEvent::Exit as before.
                    WindowEvent::CloseRequested { api, .. } => {
                        if !quitting.load(Ordering::SeqCst) {
                            shell_log(window, "close_requested -> hide");
                            api.prevent_close();
                            let _ = window.hide();
                        }
                    }
                    _ => {}
                }
            }
        })
        .setup(|app| {
            // Spawn the bundled runtime in release builds.
            // In dev, the user runs `pnpm dev:runtime` manually.
            #[cfg(not(debug_assertions))]
            {
                // A previous instance's runtime may still hold the port (its
                // bundle replaced by a rebuild → unusable); reclaim it first.
                kill_stale_sidecar();

                let runtime_exe = app
                    .path()
                    .resource_dir()
                    .expect("failed to resolve resource dir")
                    .join("resources")
                    .join("runtime")
                    .join("ginno-runtime");

                let mut cmd = Command::new(&runtime_exe);
                cmd.stdin(Stdio::null());
                if let Some(log) = open_log_file(app) {
                    let log_err = log
                        .try_clone()
                        .expect("failed to clone log file handle");
                    cmd.stdout(Stdio::from(log)).stderr(Stdio::from(log_err));
                }
                let child = cmd.spawn().unwrap_or_else(|e| {
                    panic!("failed to spawn {}: {e}", runtime_exe.display())
                });
                app.manage(RuntimeProcess(Mutex::new(Some(child))));

                // If the runtime isn't listening yet, swap the pending
                // navigation (which would hit a dead port) for the local splash
                // and flip to the app from a background task once the port
                // accepts. setup() returns immediately either way, so the event
                // loop runs and the window/splash stay responsive.
                let addr: SocketAddr = ([127, 0, 0, 1], SIDECAR_PORT).into();
                let ready_now = TcpStream::connect_timeout(&addr, Duration::from_millis(150)).is_ok();

                if !ready_now {
                    if let Some(window) = app.get_webview_window("main") {
                        let html = SPLASH_HTML.replace("__PORT__", &SIDECAR_PORT.to_string());
                        let url = format!("data:text/html;base64,{}", base64(html.as_bytes()));
                        if let Ok(url) = url.parse() {
                            let _ = window.navigate(url);
                        }
                    }
                }

                // The window is created hidden (see tauri.conf.json) so a slow
                // sidecar never paints a white / "can't connect" page. Wait for
                // the port off the main thread (a cold PyInstaller start can take
                // 30s+, which would beachball the UI if we blocked here), then
                // navigate + reveal on the main thread. navigate() also covers the
                // race where the hidden webview's implicit initial load fired
                // before the sidecar was up and got connection-refused.
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    let addr: SocketAddr = ([127, 0, 0, 1], SIDECAR_PORT).into();
                    let mut up = false;
                    for _ in 0..240 {
                        // ~60s budget; connect_timeout bounds each iteration.
                        if TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok() {
                            up = true;
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(250));
                    }
                    if !up {
                        // Budget exhausted: reveal the window on the error
                        // page (instead of today's silent dead-port page), then
                        // keep polling slowly — a late cold start or a manually
                        // started runtime recovers without user action.
                        let html = ERROR_HTML.replace("__PORT__", &SIDECAR_PORT.to_string());
                        let err_url = format!("data:text/html;base64,{}", base64(html.as_bytes()));
                        let h = handle.clone();
                        let _ = handle.run_on_main_thread(move || {
                            if let Some(w) = h.get_webview_window("main") {
                                if let Ok(u) = err_url.parse() {
                                    let _ = w.navigate(u);
                                }
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        });
                        loop {
                            std::thread::sleep(Duration::from_secs(1));
                            if TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
                            {
                                break;
                            }
                        }
                    }
                    let url = tauri::Url::parse("http://127.0.0.1:8787/")
                        .expect("sidecar url");
                    let h = handle.clone();
                    let _ = handle.run_on_main_thread(move || {
                        if let Some(w) = h.get_webview_window("main") {
                            let _ = w.navigate(url);
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    });
                });
            }
            // In dev the sidecar is run by the user; just reveal the window
            // (it loads `devUrl`). The release path reveals it once the
            // sidecar is ready (above).
            #[cfg(debug_assertions)]
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }

            // The UNUserNotificationCenter backend requires explicit
            // authorization before banners show content (the deprecated
            // NSUserNotification path did not). Release builds are a real
            // app bundle, so request once at startup — macOS shows its
            // permission prompt on first use. Dev builds are unbundled and
            // cannot post UN notifications at all; they're a no-op there.
            #[cfg(not(debug_assertions))]
            {
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    shell_log(&h, "notification auth: requesting…");
                    let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        notify_rust::request_auth_blocking()
                    }));
                    shell_log(&h, &format!("notification auth state={res:?}"));
                });
            }

            // Session/workflow completion notifications: the web UI decides
            // WHEN to notify (it knows what the user is looking at) and emits
            // ginno:notify; the shell owns the OS notification and the
            // click-to-focus round trip.
            let notify_handle = app.handle().clone();
            app.listen_any("ginno:notify", move |event| {
                let Ok(payload) = serde_json::from_str::<NotifyPayload>(event.payload()) else {
                    return;
                };
                let h = notify_handle.clone();
                shell_log(&h, &format!("ginno:notify kind={} id={}", payload.kind, payload.id));
                std::thread::spawn(move || show_notification_and_wait(h, payload));
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Ginno desktop shell");

    app.run(move |app_handle, event| {
        match event {
            // A real quit (⌘Q / menu / programmatic exit) is starting — let
            // CloseRequested destroy the window instead of hiding it.
            tauri::RunEvent::ExitRequested { .. } => {
                quitting.store(true, Ordering::SeqCst);
            }
            // Dock click while the window is hidden → bring it back
            // (macOS convention; notification clicks are handled explicitly
            // in focus_and_open).
            tauri::RunEvent::Reopen {
                has_visible_windows, ..
            } => {
                shell_log(app_handle, &format!("reopen has_visible_windows={has_visible_windows}"));
                if !has_visible_windows {
                    if let Some(w) = app_handle.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.set_focus();
                    }
                }
            }
            // Terminate the runtime when the app quits so it doesn't linger and
            // hold the port (kill_stale_sidecar would reclaim it on the next
            // start, but a clean exit is cleaner).
            tauri::RunEvent::Exit => {
                if let Some(state) = app_handle.try_state::<RuntimeProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.as_mut() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
            _ => {}
        }
    });
}
