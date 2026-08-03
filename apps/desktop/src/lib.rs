//! Tauri shell for Ginno.
//!
//! Responsibilities:
//!   1. Spawn the Python sidecar (`ginno-runtime`) on startup in release builds.
//!   2. Expose the sidecar port to the frontend via a Tauri command.
//!   3. Forward sidecar stdout/stderr to a log file under ~/.ginno/logs/.
//!
//! In dev (`tauri dev`), the user runs `pnpm dev:runtime` in a separate
//! terminal; this file only spawns the sidecar in release builds.

use std::fs::OpenOptions;
use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;
use tauri::{DragDropEvent, Manager, WindowEvent};
use tauri_plugin_shell::ShellExt;

const SIDECAR_PORT: u16 = 8787;

#[tauri::command]
fn sidecar_port() -> u16 {
    SIDECAR_PORT
}

fn open_log_file(app: &tauri::App) -> Option<std::fs::File> {
    let home = dirs_home(app);
    let logs = home.join("logs");
    std::fs::create_dir_all(&logs).ok()?;
    OpenOptions::new()
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

/// Reclaim the sidecar port from a stale `ginno-runtime`, if one holds it.
///
/// The packaged sidecar is rebuilt *in place*: if a previous app instance's
/// sidecar is still alive when a new build replaces its binary, the old
/// process keeps the port but can no longer load anything — its next lazy
/// module import reads the replaced archive and dies with a zlib error
/// (`Error -3 while decompressing data`), surfacing as broken chat turns.
/// Such a process is unrecoverable; kill it (and only it — verified by
/// process name) so the fresh sidecar can bind.
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

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // WKWebView never fires the HTML5 `ondrop` for files dragged from the
        // Finder, so the composer's JS drop handler can't see them. Handle the
        // OS-level drop natively and forward the file paths to the page via
        // `window.__ginnoFileDrop` (defined in ChatStream), which attaches them
        // through the sidecar's /api/files/attach-path endpoint.
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
            // Spawn the bundled sidecar in release builds.
            // In dev, the user runs `pnpm dev:runtime` manually.
            #[cfg(not(debug_assertions))]
            {
                // A previous instance's sidecar may still hold the port (its
                // binary replaced by a rebuild → unusable); reclaim it first.
                kill_stale_sidecar();
                let sidecar = app
                    .shell()
                    .sidecar("ginno-runtime")
                    .expect("ginno-runtime sidecar binary not bundled");
                let (mut rx, _child) = sidecar.spawn().expect("failed to spawn sidecar");

                let mut log = open_log_file(app);
                tauri::async_runtime::spawn(async move {
                    use tauri_plugin_shell::process::CommandEvent;
                    while let Some(ev) = rx.recv().await {
                        match ev {
                            CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                                if let Some(ref mut f) = log.as_mut() {
                                    let _ = f.write_all(&line);
                                    let _ = writeln!(f);
                                }
                            }
                            _ => {}
                        }
                    }
                });

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
        .invoke_handler(tauri::generate_handler![sidecar_port])
        .run(tauri::generate_context!())
        .expect("error while running Ginno desktop shell");
}
