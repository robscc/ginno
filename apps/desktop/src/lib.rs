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
//!   4. Terminate the runtime on app exit. The macOS menu bar has a Debug
//!      submenu: restart just the sidecar (release: bundled ginno-runtime;
//!      dev: `uv run uvicorn` against packages/runtime), open logs, reload
//!      the UI — so a stale/broken backend does not require quitting the app.
//!   5. Native notifications: the web UI emits `ginno:notify` when a session
//!      turn / workflow run finishes while the user looks away; the shell
//!      shows the macOS notification (optionally with a system sound, chosen
//!      in Settings → Notifications and passed per-event in the payload) and,
//!      on click, restores the window and tells the webview to navigate to
//!      the target (`__ginnoOpenSession` / `__ginnoOpenWorkflowRun`, same
//!      eval convention as `__ginnoFileDrop`). Closing the window hides it
//!      (macOS convention) so the webview and its sockets survive to keep
//!      receiving completion events.
//!   6. Floating quick-chat window ("pin", docs/floating-window-design.md):
//!      an always-on-top, undecorated, transparent second webview loading
//!      `/pin` from the same origin. Two shapes ("mini" chat box ⇄ "pill"
//!      status dot) toggled by a global hotkey, the tray icon, or the webview
//!      via commands. Rust owns window mode/geometry (~/.ginno/floating.json)
//!      and never writes settings.json (PUT /api/settings is a full-document
//!      overwrite owned by the web UI); the web UI pushes `floating` prefs
//!      down via `pin_apply_prefs`.
//!
//! In dev (`tauri dev`), the user runs `pnpm dev:runtime` in a separate
//! terminal; this file only spawns the runtime in release builds.

use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{
    DragDropEvent, Emitter, Listener, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
    menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

const SIDECAR_PORT: u16 = 8787;

/// Handle to the spawned runtime process, so `RunEvent::Exit` / Debug → Restart
/// can terminate it. Present in both release (bundled ginno-runtime) and dev
/// (`uv run uvicorn`) so the menu can restart the backend without quitting.
struct RuntimeProcess(Mutex<Option<Child>>);

/// Serialize Debug → Restart Runtime so a double-click cannot spawn two sidecars.
struct RestartLock(Mutex<()>);

/// Payload of the `ginno:notify` event emitted by the web UI (see
/// `notifyNative` in apps/web/src/lib/desktop.ts) when a session turn or
/// workflow run finishes while the user isn't looking at it.
#[derive(serde::Deserialize)]
struct NotifyPayload {
    /// `"session"`, `"workflow-run"` — decides which bridge global is called
    /// on click — or `"test"` (Settings → Notifications test button), which
    /// only refocuses the window.
    kind: String,
    /// Session id (`kind == "session"`) or run id (`kind == "workflow-run"`).
    id: String,
    title: String,
    body: String,
    /// macOS system sound name ("Glass", "Ping", …); absent = silent. The web
    /// UI reads the preference from settings.json (`notifications.sound` /
    /// `sound_name`) and passes it per-event, so the shell stays stateless.
    #[serde(default)]
    sound: Option<String>,
}

/// Sounds that ship with macOS (`/System/Library/Sounds/<name>.aiff`).
/// Allow-list doubles as sanitization: `sound` arrives over IPC from the
/// webview, so anything unknown is dropped rather than handed to the OS.
/// Keep in sync with SOUND_NAMES in apps/web/src/lib/notifyPrefs.ts.
const SYSTEM_SOUNDS: &[&str] = &[
    "Basso", "Blow", "Bottle", "Frog", "Funk", "Glass", "Hero", "Morse", "Ping", "Sosumi",
    "Submarine", "Purr", "Pop", "Tink",
];

fn open_log_file_for<M: tauri::Manager<tauri::Wry>>(app: &M) -> Option<std::fs::File> {
    let logs = ginno_home_path(app).join("logs");
    std::fs::create_dir_all(&logs).ok()?;
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("sidecar.log"))
        .ok()
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

fn sidecar_listening() -> bool {
    let addr: SocketAddr = ([127, 0, 0, 1], SIDECAR_PORT).into();
    TcpStream::connect_timeout(&addr, Duration::from_millis(150)).is_ok()
}

/// True if this pid looks like a Ginno runtime we are allowed to kill:
/// the packaged `ginno-runtime` binary, or a `uvicorn` serving
/// `ginno_runtime.server` (the `pnpm dev:runtime` / Debug-menu spawn).
fn is_ours_sidecar(pid: &str) -> bool {
    let comm = Command::new("ps")
        .args(["-p", pid, "-o", "comm="])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
        .unwrap_or_default();
    if comm.contains("ginno-runtime") {
        return true;
    }
    let args = Command::new("ps")
        .args(["-p", pid, "-o", "args="])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
        .unwrap_or_default();
    args.contains("ginno_runtime.server") || args.contains("ginno-runtime")
}

/// Kill a listener pid and, if its parent is also a Ginno sidecar (uvicorn
/// --reload supervisor / `uv run` wrapper), kill the parent first so it
/// cannot immediately respawn the worker we just stopped.
fn kill_ours(pid: &str, signal: &str) {
    if let Ok(out) = Command::new("ps").args(["-p", pid, "-o", "ppid="]).output() {
        let ppid = String::from_utf8_lossy(&out.stdout);
        let ppid = ppid.trim();
        if !ppid.is_empty() && ppid != "1" && is_ours_sidecar(ppid) {
            let mut k = Command::new("kill");
            if signal == "-9" {
                k.arg("-9");
            }
            let _ = k.arg(ppid).status();
        }
    }
    let mut k = Command::new("kill");
    if signal == "-9" {
        k.arg("-9");
    }
    let _ = k.arg(pid).status();
}

/// Reclaim the runtime port from a stale Ginno sidecar, if one holds it.
///
/// The packaged runtime is rebuilt *in place*: if a previous app instance's
/// runtime is still alive when a new build replaces its files, the old process
/// keeps the port but can no longer load anything from the replaced bundle,
/// surfacing as broken chat turns. Such a process is unrecoverable; kill it
/// (and only it — verified by process name / cmdline) so the fresh runtime can
/// bind. Also used by Debug → Restart Runtime.
fn kill_stale_sidecar() {
    if !sidecar_listening() {
        return; // port already free
    }
    let Ok(list) = Command::new("lsof")
        .args(["-t", &format!("-iTCP:{SIDECAR_PORT}"), "-sTCP:LISTEN"])
        .output()
    else {
        return;
    };
    for pid in String::from_utf8_lossy(&list.stdout).split_whitespace() {
        // Never kill a stranger on the port — only a Ginno runtime of ours.
        if is_ours_sidecar(pid) {
            kill_ours(pid, "");
        }
    }
    // Wait for the port to free up; escalate to SIGKILL once if it lingers.
    let mut escalated = false;
    for _ in 0..30 {
        std::thread::sleep(Duration::from_millis(100));
        if !sidecar_listening() {
            return;
        }
        if !escalated {
            escalated = true;
            if let Ok(list) = Command::new("lsof")
                .args(["-t", &format!("-iTCP:{SIDECAR_PORT}"), "-sTCP:LISTEN"])
                .output()
            {
                for pid in String::from_utf8_lossy(&list.stdout).split_whitespace() {
                    if is_ours_sidecar(pid) {
                        kill_ours(pid, "-9");
                    }
                }
            }
        }
    }
}

fn ginno_home_path<M: tauri::Manager<tauri::Wry>>(app: &M) -> std::path::PathBuf {
    if let Ok(p) = std::env::var("GINNO_HOME") {
        return std::path::PathBuf::from(p);
    }
    app.path()
        .home_dir()
        .map(|h| h.join(".ginno"))
        .unwrap_or_else(|_| std::path::PathBuf::from(".ginno"))
}

/// Open a path in Finder (macOS) / Explorer / xdg-open. Best-effort.
fn reveal_path(path: &std::path::Path) {
    let _ = std::fs::create_dir_all(path);
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg(path).status();
    }
    #[cfg(target_os = "windows")]
    {
        let _ = Command::new("explorer").arg(path).status();
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = Command::new("xdg-open").arg(path).status();
    }
}

fn open_in_editor(path: &std::path::Path) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if !path.exists() {
        let _ = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path);
    }
    #[cfg(target_os = "macos")]
    {
        // `-t` = default text editor (TextEdit / whatever the user set).
        let _ = Command::new("open").args(["-t"]).arg(path).status();
    }
    #[cfg(not(target_os = "macos"))]
    {
        reveal_path(path.parent().unwrap_or(path));
    }
}

/// Resolve `packages/runtime` so `tauri dev` can spawn uvicorn against source.
/// Walks up from CARGO_MANIFEST_DIR / current_exe until we find it.
fn runtime_src_dir() -> Option<std::path::PathBuf> {
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    candidates.push(std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(p) = exe.parent() {
            candidates.push(p.to_path_buf());
        }
    }
    for start in candidates {
        let mut cur = start;
        for _ in 0..8 {
            let hit = cur.join("packages").join("runtime");
            if hit.join("src").join("ginno_runtime").is_dir() {
                return Some(hit);
            }
            if !cur.pop() {
                break;
            }
        }
    }
    None
}

fn spawn_sidecar<M: tauri::Manager<tauri::Wry>>(app: &M) -> Result<Child, String> {
    let mut cmd = if cfg!(debug_assertions) {
        let runtime = runtime_src_dir().ok_or_else(|| {
            "cannot find packages/runtime (run `tauri dev` from the ginno repo)".to_string()
        })?;
        let mut cmd = Command::new("uv");
        cmd.current_dir(&runtime).args([
            "run",
            "uvicorn",
            "ginno_runtime.server:app",
            "--port",
            &SIDECAR_PORT.to_string(),
        ]);
        cmd
    } else {
        let runtime_exe = app
            .path()
            .resource_dir()
            .map_err(|e| format!("resource dir: {e}"))?
            .join("resources")
            .join("runtime")
            .join("ginno-runtime");
        Command::new(runtime_exe)
    };
    cmd.stdin(Stdio::null());
    if let Some(log) = open_log_file_for(app) {
        let log_err = log
            .try_clone()
            .map_err(|e| format!("clone log handle: {e}"))?;
        cmd.stdout(Stdio::from(log)).stderr(Stdio::from(log_err));
    }
    cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))
}

fn wait_for_sidecar(timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if sidecar_listening() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    sidecar_listening()
}

/// Kill the tracked child (and any leftover listener on :8787), then spawn a
/// fresh sidecar. Reloads the webview onto splash → app once the port accepts.
fn restart_sidecar(app: &tauri::AppHandle) {
    shell_log(app, "debug: restart runtime requested");
    // Splash first so the webview is not sitting on a dying origin.
    #[cfg(not(debug_assertions))]
    if let Some(window) = app.get_webview_window("main") {
        let html = SPLASH_HTML.replace("__PORT__", &SIDECAR_PORT.to_string());
        let url = format!("data:text/html;base64,{}", base64(html.as_bytes()));
        if let Ok(url) = url.parse() {
            let _ = window.navigate(url);
        }
    }

    if let Some(state) = app.try_state::<RuntimeProcess>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
    kill_stale_sidecar();

    match spawn_sidecar(app) {
        Ok(child) => {
            if let Some(state) = app.try_state::<RuntimeProcess>() {
                if let Ok(mut guard) = state.0.lock() {
                    *guard = Some(child);
                }
            }
        }
        Err(e) => {
            shell_log(app, &format!("debug: spawn sidecar FAILED: {e}"));
            return;
        }
    }

    // Caller already runs on a background thread and holds RestartLock for
    // this whole function, so wait here (don't spawn another waiter).
    let up = wait_for_sidecar(Duration::from_secs(60));
    if !up {
        shell_log(app, "debug: sidecar did not come up within 60s");
        #[cfg(not(debug_assertions))]
        {
            let html = ERROR_HTML.replace("__PORT__", &SIDECAR_PORT.to_string());
            let err_url = format!("data:text/html;base64,{}", base64(html.as_bytes()));
            let h = app.clone();
            let _ = app.run_on_main_thread(move || {
                if let Some(w) = h.get_webview_window("main") {
                    if let Ok(u) = err_url.parse() {
                        let _ = w.navigate(u);
                    }
                }
            });
        }
        return;
    }
    // Release: sidecar hosts the UI, so navigate back onto :8787.
    // Dev: the webview stays on :3000 (Next); just bring the window forward
    // and let ChatStream's existing reconnect loop pick the new sidecar up.
    #[cfg(not(debug_assertions))]
    {
        let url = match tauri::Url::parse(&format!("http://127.0.0.1:{SIDECAR_PORT}/")) {
            Ok(u) => u,
            Err(_) => return,
        };
        let h = app.clone();
        let _ = app.run_on_main_thread(move || {
            if let Some(w) = h.get_webview_window("main") {
                let _ = w.navigate(url);
                let _ = w.show();
                let _ = w.set_focus();
            }
        });
    }
    #[cfg(debug_assertions)]
    {
        let h = app.clone();
        let _ = app.run_on_main_thread(move || {
            if let Some(w) = h.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        });
    }
    shell_log(app, "debug: sidecar restarted");
}

fn install_debug_menu(app: &tauri::App) -> tauri::Result<()> {
    let restart = MenuItem::with_id(
        app,
        "debug-restart-runtime",
        "重启后端",
        true,
        Some("CmdOrCtrl+Alt+R"),
    )?;
    let sidecar_log = MenuItem::with_id(app, "debug-open-sidecar-log", "打开 sidecar 日志", true, None::<&str>)?;
    let shell_log_item = MenuItem::with_id(app, "debug-open-shell-log", "打开 shell 日志", true, None::<&str>)?;
    let logs_dir = MenuItem::with_id(app, "debug-reveal-logs", "在访达中显示日志目录", true, None::<&str>)?;
    let home_dir = MenuItem::with_id(app, "debug-reveal-home", "在访达中显示 ~/.ginno", true, None::<&str>)?;
    let reload = MenuItem::with_id(app, "debug-reload-ui", "重新加载界面", true, Some("CmdOrCtrl+R"))?;

    let debug = Submenu::with_id_and_items(
        app,
        "debug",
        "Debug",
        true,
        &[
            &restart,
            &PredefinedMenuItem::separator(app)?,
            &sidecar_log,
            &shell_log_item,
            &logs_dir,
            &home_dir,
            &PredefinedMenuItem::separator(app)?,
            &reload,
        ],
    )?;

    // Default menu (Ginno / File / Edit / View / Window / Help) plus Debug.
    let menu = Menu::default(app.handle())?;
    menu.append(&debug)?;
    app.set_menu(menu)?;
    Ok(())
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
fn focus_and_open(app: &tauri::AppHandle, kind: &str, id: &str) {
    shell_log(app, &format!("focus_and_open kind={kind} id={id}"));
    // "test" (Settings → Notifications test button): refocus only — there is
    // no target to navigate to.
    let script = if kind == "test" {
        String::new()
    } else if kind == "workflow-run" {
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

// ---------------------------------------------------------------------------
// Floating quick-chat window ("pin") — docs/floating-window-design.md §2/§4
// ---------------------------------------------------------------------------

const PIN_LABEL: &str = "pin";
/// Pill (collapsed) shape, logical px. Just a status dot + name.
const PILL_W: f64 = 148.0;
const PILL_H: f64 = 44.0;
/// Mini (chat box) default size, logical px.
const MINI_W: f64 = 360.0;
const MINI_H: f64 = 520.0;
// ⇧⌘Space. NOT ⌃⌥Space: that one is macOS's "select previous input source"
// and loses to the system shortcut (a guaranteed conflict for IME users).
// Also avoids ⌘Space (Spotlight) and ⌥Space (Raycast/Alfred defaults).
const DEFAULT_HOTKEY: &str = "CommandOrControl+Shift+Space";

#[derive(serde::Serialize, serde::Deserialize, Clone, Copy, Default)]
struct Geo {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Copy, Default)]
struct PillGeo {
    x: f64,
    y: f64,
}

/// ~/.ginno/floating.json — Rust-owned window geometry (settings.json is
/// full-document-overwritten by the web UI, so never touch it from here).
#[derive(serde::Serialize, serde::Deserialize, Default)]
struct FloatingGeo {
    #[serde(default)]
    mini: Option<Geo>,
    #[serde(default)]
    pill: Option<PillGeo>,
}

/// Mirror of the `floating` key in settings.json (subset the shell acts on).
/// Pushed by the web UI via `pin_apply_prefs`; also read once at startup so
/// the hotkey works before any webview is up.
#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct PinPrefs {
    #[serde(default = "default_hotkey")]
    hotkey: String,
    #[serde(default = "default_true")]
    visible_on_all_spaces: bool,
    /// "avoid" (default macOS behavior: never join other apps' fullscreen
    /// spaces) | "overlay" (add FullScreenAuxiliary to float above them).
    #[serde(default = "default_fullscreen_policy")]
    fullscreen_policy: String,
    #[serde(default)]
    pill_click_through: bool,
}

fn default_hotkey() -> String {
    DEFAULT_HOTKEY.to_string()
}
fn default_true() -> bool {
    true
}
fn default_fullscreen_policy() -> String {
    "avoid".to_string()
}

impl Default for PinPrefs {
    fn default() -> Self {
        Self {
            hotkey: default_hotkey(),
            visible_on_all_spaces: true,
            fullscreen_policy: default_fullscreen_policy(),
            pill_click_through: false,
        }
    }
}

struct PinState {
    /// "mini" | "pill" — Rust is the source of truth; webview mirrors via
    /// the `pin:mode` event / `pin_get_mode`.
    mode: Mutex<String>,
    geo: Mutex<FloatingGeo>,
    geo_path: std::path::PathBuf,
    prefs: Mutex<PinPrefs>,
    /// Whether the CURRENT prefs hotkey is actually registered with the OS.
    /// False = taken by another app / unparsable; tray menu still works.
    /// Surfaced to Settings → 悬浮窗 via `pin_hotkey_status`.
    hotkey_active: AtomicBool,
}

fn load_floating_geo(home: &std::path::Path) -> FloatingGeo {
    std::fs::read_to_string(home.join("floating.json"))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

/// Read the `floating` prefs from settings.json, tolerating a missing key
/// (existing users' settings.json predates it and PUT only rewrites on save).
fn load_pin_prefs(home: &std::path::Path) -> PinPrefs {
    let defaults = PinPrefs::default();
    let Ok(text) = std::fs::read_to_string(home.join("settings.json")) else {
        return defaults;
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else {
        return defaults;
    };
    let Some(f) = v.get("floating") else {
        return defaults;
    };
    PinPrefs {
        hotkey: f
            .get("hotkey")
            .and_then(|x| x.as_str())
            .filter(|s| !s.trim().is_empty())
            .map(String::from)
            .unwrap_or(defaults.hotkey),
        visible_on_all_spaces: f
            .get("visible_on_all_spaces")
            .and_then(|x| x.as_bool())
            .unwrap_or(defaults.visible_on_all_spaces),
        fullscreen_policy: f
            .get("fullscreen_policy")
            .and_then(|x| x.as_str())
            .map(String::from)
            .unwrap_or(defaults.fullscreen_policy),
        pill_click_through: f
            .get("pill_click_through")
            .and_then(|x| x.as_bool())
            .unwrap_or(defaults.pill_click_through),
    }
}

fn floating_setting_bool(home: &std::path::Path, key: &str, default: bool) -> bool {
    let Ok(text) = std::fs::read_to_string(home.join("settings.json")) else {
        return default;
    };
    serde_json::from_str::<serde_json::Value>(&text)
        .ok()
        .and_then(|v| v.get("floating").and_then(|f| f.get(key)).and_then(|x| x.as_bool()))
        .unwrap_or(default)
}

fn persist_pin_geo(app: &tauri::AppHandle) {
    let Some(st) = app.try_state::<PinState>() else {
        return;
    };
    let text = {
        let geo = st.geo.lock().unwrap();
        serde_json::to_string_pretty(&*geo)
    };
    if let Ok(text) = text {
        let _ = std::fs::write(&st.geo_path, text);
    }
}

/// Current monitor bounds in logical px: (x, y, w, h).
fn monitor_logical(w: &WebviewWindow) -> Option<(f64, f64, f64, f64)> {
    let sf = w.scale_factor().ok()?;
    let m = w
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| w.primary_monitor().ok().flatten())?;
    Some((
        m.position().x as f64 / sf,
        m.position().y as f64 / sf,
        m.size().width as f64 / sf,
        m.size().height as f64 / sf,
    ))
}

fn default_mini_geo(w: &WebviewWindow) -> Geo {
    match monitor_logical(w) {
        Some((mx, my, mw, mh)) => Geo {
            x: mx + mw - MINI_W - 32.0,
            y: my + 96.0,
            w: MINI_W,
            h: MINI_H.min(mh - 140.0),
        },
        None => Geo {
            x: 120.0,
            y: 120.0,
            w: MINI_W,
            h: MINI_H,
        },
    }
}

fn default_pill_geo(w: &WebviewWindow) -> PillGeo {
    match monitor_logical(w) {
        Some((mx, my, mw, mh)) => PillGeo {
            x: mx + mw - PILL_W - 24.0,
            y: my + mh - PILL_H - 24.0,
        },
        None => PillGeo { x: 200.0, y: 200.0 },
    }
}

/// Edge snap + keep-on-screen clamp for the mini shape (16px snap zone;
/// top snaps below the menu bar; the notch area is inside the menu bar band).
fn snap_mini_geo(w: &WebviewWindow, g: &mut Geo) {
    let Some((mx, my, mw, mh)) = monitor_logical(w) else {
        return;
    };
    const SNAP: f64 = 16.0;
    const MENU_BAR: f64 = 28.0;
    if (g.x - mx).abs() < SNAP {
        g.x = mx;
    }
    if (g.x + g.w - (mx + mw)).abs() < SNAP {
        g.x = mx + mw - g.w;
    }
    if (g.y - my).abs() < SNAP {
        g.y = my + MENU_BAR;
    }
    if (g.y + g.h - (my + mh)).abs() < SNAP {
        g.y = my + mh - g.h;
    }
    g.x = g.x.clamp(mx - g.w + 100.0, mx + mw - 100.0);
    g.y = g.y.clamp(my + MENU_BAR - 8.0, my + mh - 48.0);
}

/// Snapshot the live window geometry into the slot for the current mode.
fn capture_pin_geo(w: &WebviewWindow, st: &PinState) {
    let mode = st.mode.lock().unwrap().clone();
    let (Ok(sf), Ok(pos), Ok(size)) = (w.scale_factor(), w.outer_position(), w.inner_size()) else {
        return;
    };
    let x = pos.x as f64 / sf;
    let y = pos.y as f64 / sf;
    if mode == "pill" {
        st.geo.lock().unwrap().pill = Some(PillGeo { x, y });
    } else {
        let mut g = Geo {
            x,
            y,
            w: size.width as f64 / sf,
            h: size.height as f64 / sf,
        };
        snap_mini_geo(w, &mut g);
        st.geo.lock().unwrap().mini = Some(g);
    }
}

fn apply_pin_mode_geometry(app: &tauri::AppHandle, w: &WebviewWindow) {
    let Some(st) = app.try_state::<PinState>() else {
        return;
    };
    let mode = st.mode.lock().unwrap().clone();
    if mode == "pill" {
        let pos = st
            .geo
            .lock()
            .unwrap()
            .pill
            .unwrap_or_else(|| default_pill_geo(w));
        let _ = w.set_resizable(false);
        let _ = w.set_size(tauri::LogicalSize::new(PILL_W, PILL_H));
        let _ = w.set_position(tauri::LogicalPosition::new(pos.x, pos.y));
    } else {
        let g = st
            .geo
            .lock()
            .unwrap()
            .mini
            .unwrap_or_else(|| default_mini_geo(w));
        let _ = w.set_resizable(true);
        let _ = w.set_size(tauri::LogicalSize::new(g.w, g.h));
        let _ = w.set_position(tauri::LogicalPosition::new(g.x, g.y));
    }
}

/// Spaces/fullscreen behavior + pill click-through, per current prefs & mode.
fn apply_pin_window_prefs(app: &tauri::AppHandle, w: &WebviewWindow) {
    let Some(st) = app.try_state::<PinState>() else {
        return;
    };
    let (mode, prefs) = {
        (
            st.mode.lock().unwrap().clone(),
            st.prefs.lock().unwrap().clone(),
        )
    };
    apply_window_collection_behavior(w, &prefs);
    let _ = w.set_ignore_cursor_events(mode == "pill" && prefs.pill_click_through);
}

#[cfg(target_os = "macos")]
fn apply_window_collection_behavior(w: &WebviewWindow, prefs: &PinPrefs) {
    use objc::{msg_send, sel, sel_impl};
    // NSWindowCollectionBehavior: CanJoinAllSpaces = 1<<0 (present on every
    // regular space), FullScreenAuxiliary = 1<<8 (also float above other
    // apps' fullscreen spaces — the "overlay" policy).
    let Ok(ns) = w.ns_window() else {
        return;
    };
    let ns = ns as *mut objc::runtime::Object;
    if ns.is_null() {
        return;
    }
    let mut behavior: u64 = 0;
    if prefs.visible_on_all_spaces {
        behavior |= 1 << 0;
    }
    if prefs.fullscreen_policy == "overlay" {
        behavior |= 1 << 8;
    }
    unsafe {
        let _: () = msg_send![ns, setCollectionBehavior: behavior];
    }
}

#[cfg(not(target_os = "macos"))]
fn apply_window_collection_behavior(w: &WebviewWindow, prefs: &PinPrefs) {
    let _ = w.set_visible_on_all_workspaces(prefs.visible_on_all_spaces);
}

/// Lazily create the pin window (hidden). Dev loads the Next dev server,
/// release loads the sidecar origin — same split as `frontendDist`/`devUrl`.
fn ensure_pin_window(app: &tauri::AppHandle) -> tauri::Result<WebviewWindow> {
    if let Some(w) = app.get_webview_window(PIN_LABEL) {
        return Ok(w);
    }
    let url = if cfg!(debug_assertions) {
        "http://localhost:3000/pin"
    } else {
        "http://127.0.0.1:8787/pin"
    };
    let url = tauri::Url::parse(url).map_err(|e| tauri::Error::InvalidUrl(e))?;
    let w = WebviewWindowBuilder::new(app, PIN_LABEL, WebviewUrl::External(url))
        .title("Ginno 悬浮窗")
        .inner_size(MINI_W, MINI_H)
        .min_inner_size(280.0, 320.0)
        .max_inner_size(480.0, 900.0)
        .decorations(false)
        .transparent(true)
        .shadow(true)
        .always_on_top(true)
        .resizable(true)
        .skip_taskbar(true)
        .accept_first_mouse(true)
        .visible(false)
        .build()?;
    apply_pin_window_prefs(app, &w);
    Ok(w)
}

fn set_pin_mode(app: &tauri::AppHandle, mode: &str) {
    if mode != "mini" && mode != "pill" {
        return;
    }
    let Some(w) = app.get_webview_window(PIN_LABEL) else {
        return;
    };
    let Some(st) = app.try_state::<PinState>() else {
        return;
    };
    if st.mode.lock().unwrap().as_str() == mode {
        return;
    }
    capture_pin_geo(&w, &st); // bank the outgoing shape's geometry first
    *st.mode.lock().unwrap() = mode.to_string();
    apply_pin_mode_geometry(app, &w);
    apply_pin_window_prefs(app, &w);
    let _ = w.emit("pin:mode", serde_json::json!({ "mode": mode }));
    persist_pin_geo(app);
    if mode == "mini" && w.is_visible().unwrap_or(false) {
        let _ = w.set_focus();
    }
}

fn toggle_pin_now(app: &tauri::AppHandle) {
    let w = match ensure_pin_window(app) {
        Ok(w) => w,
        Err(e) => {
            shell_log(app, &format!("ensure_pin_window FAILED: {e}"));
            return;
        }
    };
    if w.is_visible().unwrap_or(false) {
        if let Some(st) = app.try_state::<PinState>() {
            capture_pin_geo(&w, &st);
        }
        let _ = w.hide();
        persist_pin_geo(app);
        shell_log(app, "pin: hide");
    } else {
        apply_pin_mode_geometry(app, &w);
        apply_pin_window_prefs(app, &w);
        let _ = w.show();
        let mode = app
            .try_state::<PinState>()
            .map(|s| s.mode.lock().unwrap().clone())
            .unwrap_or_else(|| "mini".to_string());
        if mode == "mini" {
            let _ = w.set_focus();
        }
        shell_log(app, &format!("pin: show mode={mode}"));
    }
}

fn toggle_pin(app: &tauri::AppHandle) {
    // Release builds serve /pin from the sidecar; before it is up the window
    // would paint a connection-refused page. Defer until the port accepts.
    #[cfg(not(debug_assertions))]
    if !sidecar_listening() {
        shell_log(app, "pin toggle deferred: sidecar not up yet");
        let h = app.clone();
        std::thread::spawn(move || {
            if wait_for_sidecar(Duration::from_secs(90)) {
                let h2 = h.clone();
                let _ = h.run_on_main_thread(move || toggle_pin_now(&h2));
            }
        });
        return;
    }
    toggle_pin_now(app);
}

/// Register `hotkey`, recording the outcome in PinState::hotkey_active.
/// Returns true when the shortcut is now live with the OS.
fn register_pin_hotkey(app: &tauri::AppHandle, hotkey: &str) -> bool {
    let ok = match hotkey.parse::<Shortcut>() {
        Ok(sc) => match app.global_shortcut().register(sc) {
            Ok(()) => {
                shell_log(app, &format!("pin hotkey registered: {hotkey}"));
                true
            }
            Err(e) => {
                shell_log(
                    app,
                    &format!("pin hotkey register FAILED: {e} (tray menu still works; change it in Settings → 悬浮窗)"),
                );
                false
            }
        },
        Err(e) => {
            shell_log(app, &format!("pin hotkey parse FAILED: {hotkey}: {e}"));
            false
        }
    };
    if let Some(st) = app.try_state::<PinState>() {
        st.hotkey_active.store(ok, Ordering::Relaxed);
    }
    ok
}

#[tauri::command]
fn pin_toggle(app: tauri::AppHandle) {
    toggle_pin(&app);
}

#[tauri::command]
fn pin_set_mode(app: tauri::AppHandle, mode: String) {
    set_pin_mode(&app, &mode);
}

#[tauri::command]
fn pin_get_mode(app: tauri::AppHandle) -> String {
    app.try_state::<PinState>()
        .map(|s| s.mode.lock().unwrap().clone())
        .unwrap_or_else(|| "mini".to_string())
}

#[tauri::command]
fn pin_hide(app: tauri::AppHandle) {
    if let Some(w) = app.get_webview_window(PIN_LABEL) {
        if let Some(st) = app.try_state::<PinState>() {
            capture_pin_geo(&w, &st);
        }
        let _ = w.hide();
        persist_pin_geo(&app);
    }
}

/// ⌘↵ "take over": show + focus the main window, optionally navigating it
/// to a session via the existing `__ginnoOpenSession` eval bridge.
#[tauri::command]
fn pin_open_main(app: tauri::AppHandle, session_id: Option<String>) {
    match session_id.filter(|s| !s.is_empty()) {
        Some(sid) => focus_and_open(&app, "session", &sid),
        None => {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
    }
}

/// The web UI pushes its `floating` settings down (read-modify-write of
/// settings.json stays entirely on the web side). Returns whether the
/// (possibly new) hotkey is now live — the UI uses this to warn about
/// conflicts (taken shortcut / bad syntax) instead of failing silently.
#[tauri::command]
fn pin_apply_prefs(app: tauri::AppHandle, prefs: PinPrefs) -> bool {
    let Some(st) = app.try_state::<PinState>() else {
        return false;
    };
    let old = {
        let mut g = st.prefs.lock().unwrap();
        std::mem::replace(&mut *g, prefs.clone())
    };
    // Re-register when the hotkey changed; also retry an unchanged one that
    // never went live (e.g. it was taken at launch but has since been freed).
    let hotkey_ok = if old.hotkey != prefs.hotkey {
        if let Ok(sc) = old.hotkey.parse::<Shortcut>() {
            let _ = app.global_shortcut().unregister(sc);
        }
        register_pin_hotkey(&app, &prefs.hotkey)
    } else {
        st.hotkey_active.load(Ordering::Relaxed)
            || register_pin_hotkey(&app, &prefs.hotkey)
    };
    if let Some(w) = app.get_webview_window(PIN_LABEL) {
        apply_pin_window_prefs(&app, &w);
    }
    hotkey_ok
}

/// Settings → 悬浮窗 reads this on load to show whether the current hotkey
/// actually registered (false ⇒ occupied by another app or invalid).
#[tauri::command]
fn pin_hotkey_status(app: tauri::AppHandle) -> bool {
    app.try_state::<PinState>()
        .map(|st| st.hotkey_active.load(Ordering::Relaxed))
        .unwrap_or(false)
}

/// Menu-bar tray (decision Q4: tray icon + Dock stays). Left click toggles
/// the pin window; the menu offers explicit entries + quit.
fn install_tray(app: &tauri::App) -> tauri::Result<()> {
    let toggle = MenuItem::with_id(app, "tray-pin-toggle", "显示/隐藏悬浮窗", true, None::<&str>)?;
    let open_main = MenuItem::with_id(app, "tray-open-main", "打开主窗口", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray-quit", "退出 Ginno", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let items: &[&dyn IsMenuItem<tauri::Wry>] = &[&toggle, &open_main, &sep, &quit];
    let menu = Menu::with_items(app, items)?;
    let mut tray = TrayIconBuilder::with_id("ginno-tray")
        .tooltip("Ginno")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray-pin-toggle" => toggle_pin(app),
            "tray-open-main" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                }
            }
            // app.exit runs ExitRequested (flips `quitting`) then Exit (kills
            // the sidecar) — same path as ⌘Q.
            "tray-quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_pin(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

pub fn run() {
    // Set while a real quit is in flight (⌘Q / menu / ExitRequested) so the
    // CloseRequested handler below destroys the window instead of hiding it.
    let quitting = Arc::new(AtomicBool::new(false));

    let app = tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    // One app-wide hotkey (the pin toggle); ignore key-release.
                    if event.state() == ShortcutState::Pressed {
                        toggle_pin(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            pin_toggle,
            pin_set_mode,
            pin_get_mode,
            pin_hide,
            pin_open_main,
            pin_apply_prefs,
            pin_hotkey_status
        ])
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            match id {
                "debug-restart-runtime" => {
                    let app = app.clone();
                    std::thread::spawn(move || {
                        let Some(lock) = app.try_state::<RestartLock>() else {
                            return;
                        };
                        let Ok(_guard) = lock.0.try_lock() else {
                            shell_log(&app, "debug: restart already in flight");
                            return;
                        };
                        restart_sidecar(&app);
                    });
                }
                "debug-open-sidecar-log" => {
                    open_in_editor(&ginno_home_path(app).join("logs").join("sidecar.log"));
                }
                "debug-open-shell-log" => {
                    open_in_editor(&ginno_home_path(app).join("logs").join("shell.log"));
                }
                "debug-reveal-logs" => {
                    reveal_path(&ginno_home_path(app).join("logs"));
                }
                "debug-reveal-home" => {
                    reveal_path(&ginno_home_path(app));
                }
                "debug-reload-ui" => {
                    if let Some(w) = app.get_webview_window("main") {
                        #[cfg(not(debug_assertions))]
                        if sidecar_listening() {
                            if let Ok(url) =
                                format!("http://127.0.0.1:{SIDECAR_PORT}/").parse()
                            {
                                let _ = w.navigate(url);
                            }
                        }
                        #[cfg(debug_assertions)]
                        {
                            let _ = w.eval("location.reload()");
                        }
                    }
                }
                _ => {}
            }
        })
        // WKWebView never fires the HTML5 `ondrop` for files dragged from the
        // Finder, so the composer's JS drop handler can't see them. Handle the
        // OS-level drop natively and forward the file paths to the page via
        // `window.__ginnoFileDrop` (defined in ChatStream), which attaches them
        // through the runtime's /api/files/attach-path endpoint.
        .on_window_event({
            let quitting = quitting.clone();
            move |window, event| {
                let label = window.label();
                match event {
                    WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                        // File drops bridge into the main window's composer
                        // only; the pin window has drag-drop disabled.
                        if label != "main" || paths.is_empty() {
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
                            shell_log(window, &format!("close_requested -> hide ({label})"));
                            api.prevent_close();
                            if label == PIN_LABEL {
                                if let (Some(st), Some(wv)) = (
                                    window.try_state::<PinState>(),
                                    window.get_webview_window(PIN_LABEL),
                                ) {
                                    capture_pin_geo(&wv, &st);
                                }
                                persist_pin_geo(window.app_handle());
                            }
                            let _ = window.hide();
                        }
                    }
                    _ => {}
                }
                // Pin window: track geometry as the user drags/resizes (memory
                // only) and flush to ~/.ginno/floating.json when it loses
                // focus — per-event disk writes during a drag are wasteful.
                if label == PIN_LABEL {
                    match event {
                        WindowEvent::Moved(_) | WindowEvent::Resized(_) => {
                            if let (Some(st), Some(wv)) = (
                                window.try_state::<PinState>(),
                                window.get_webview_window(PIN_LABEL),
                            ) {
                                capture_pin_geo(&wv, &st);
                            }
                        }
                        WindowEvent::Focused(false) => {
                            if let (Some(st), Some(wv)) = (
                                window.try_state::<PinState>(),
                                window.get_webview_window(PIN_LABEL),
                            ) {
                                capture_pin_geo(&wv, &st);
                            }
                            persist_pin_geo(window.app_handle());
                        }
                        _ => {}
                    }
                }
            }
        })
        .setup(|app| {
            app.manage(RuntimeProcess(Mutex::new(None)));
            app.manage(RestartLock(Mutex::new(())));
            if let Err(e) = install_debug_menu(app) {
                shell_log(app, &format!("install_debug_menu FAILED: {e}"));
            }

            // Floating quick-chat window: state, tray, global hotkey.
            let ginno_home = ginno_home_path(app);
            let prefs = load_pin_prefs(&ginno_home);
            let show_on_launch = floating_setting_bool(&ginno_home, "show_on_launch", false);
            app.manage(PinState {
                mode: Mutex::new("mini".to_string()),
                geo: Mutex::new(load_floating_geo(&ginno_home)),
                geo_path: ginno_home.join("floating.json"),
                prefs: Mutex::new(prefs.clone()),
                hotkey_active: AtomicBool::new(false),
            });
            if let Err(e) = install_tray(app) {
                shell_log(app, &format!("install_tray FAILED: {e}"));
            }
            register_pin_hotkey(app.handle(), &prefs.hotkey);
            if show_on_launch {
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    // Release: /pin is served by the sidecar — wait for it.
                    // Dev: give the Next dev server a moment to come up.
                    #[cfg(not(debug_assertions))]
                    wait_for_sidecar(Duration::from_secs(120));
                    #[cfg(debug_assertions)]
                    std::thread::sleep(Duration::from_secs(2));
                    let h2 = h.clone();
                    let _ = h.run_on_main_thread(move || {
                        let visible = h2
                            .get_webview_window(PIN_LABEL)
                            .map(|w| w.is_visible().unwrap_or(false))
                            .unwrap_or(false);
                        if !visible {
                            toggle_pin_now(&h2);
                        }
                    });
                });
            }

            // Spawn the bundled runtime in release builds.
            // In dev, the user runs `pnpm dev:runtime` (or uses Debug → 重启后端).
            #[cfg(not(debug_assertions))]
            {
                // A previous instance's runtime may still hold the port (its
                // bundle replaced by a rebuild → unusable); reclaim it first.
                kill_stale_sidecar();
                match spawn_sidecar(app) {
                    Ok(child) => {
                        if let Some(state) = app.try_state::<RuntimeProcess>() {
                            if let Ok(mut guard) = state.0.lock() {
                                *guard = Some(child);
                            }
                        }
                    }
                    Err(e) => {
                        panic!("failed to spawn sidecar: {e}");
                    }
                }

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
            // In dev the sidecar is run by the user (or Debug → 重启后端);
            // just reveal the window (it loads `devUrl`). The release path
            // reveals it once the sidecar is ready (above).
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
                persist_pin_geo(app_handle);
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
