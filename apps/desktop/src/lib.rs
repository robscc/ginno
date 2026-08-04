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
//!
//! In dev (`tauri dev`), the user runs `pnpm dev:runtime` in a separate
//! terminal; this file only spawns the runtime in release builds.

use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{DragDropEvent, Manager, WindowEvent};

const SIDECAR_PORT: u16 = 8787;

/// Handle to the spawned runtime process, so `RunEvent::Exit` can terminate it.
struct RuntimeProcess(Mutex<Option<Child>>);

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

pub fn run() {
    let app = tauri::Builder::default()
        // WKWebView never fires the HTML5 `ondrop` for files dragged from the
        // Finder, so the composer's JS drop handler can't see them. Handle the
        // OS-level drop natively and forward the file paths to the page via
        // `window.__ginnoFileDrop` (defined in ChatStream), which attaches them
        // through the runtime's /api/files/attach-path endpoint.
        .on_window_event(|window, event| {
            if let WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) = event {
                if paths.is_empty() {
                    return;
                }
                if let Some(webview) = window.get_webview_window("main") {
                    let paths_json =
                        serde_json::to_string(paths).unwrap_or_else(|_| "[]".to_string());
                    let _ = webview.eval(&format!(
                        "window.__ginnoFileDrop && window.__ginnoFileDrop({paths_json});"
                    ));
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
                    for _ in 0..240 {
                        // ~60s budget; connect_timeout bounds each iteration.
                        if TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok() {
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(250));
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
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Ginno desktop shell");

    app.run(|app_handle, event| {
        // Terminate the runtime when the app quits so it doesn't linger and
        // hold the port (kill_stale_sidecar would reclaim it on the next
        // start, but a clean exit is cleaner).
        if let tauri::RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<RuntimeProcess>() {
                if let Ok(mut guard) = state.0.lock() {
                    if let Some(child) = guard.as_mut() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        }
    });
}
