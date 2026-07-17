//! Tauri shell for Ginno.
//!
//! Responsibilities:
//!   1. Spawn the Python sidecar (`ginno-runtime`) on startup.
//!   2. Expose the sidecar port to the frontend via a Tauri command.
//!   3. Forward sidecar stdout/stderr to a log file under ~/.ginno/logs/.
//!
//! In dev (`tauri dev`), the sidecar can be started manually via
//! `pnpm dev:runtime`. In release builds, Tauri bundles the sidecar
//! binary as an `externalBin` resource and spawns it here.

use std::fs::OpenOptions;
use std::io::Write;
use tauri::Manager;
use tauri_plugin_shell::process::Command;
use tauri_plugin_shell::ShellExt;

const SIDECAR_PORT: u16 = 8787;

#[tauri::command]
fn sidecar_port() -> u16 {
    SIDECAR_PORT
}

fn open_log_file(app: &tauri::App) -> std::io::Result<std::fs::File> {
    let home = dirs_home(app);
    let logs = home.join("logs");
    std::fs::create_dir_all(&logs)?;
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("sidecar.log"))
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

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Spawn the bundled sidecar in release builds.
            // In dev, the user runs `pnpm dev:runtime` manually.
            #[cfg(not(debug_assertions))]
            {
                let sidecar = app
                    .shell()
                    .sidecar("ginno-runtime")
                    .expect("ginno-runtime sidecar binary not bundled");
                let (mut rx, _child) = sidecar.spawn().expect("failed to spawn sidecar");

                let mut log = open_log_file(app).ok();
                tauri::async_runtime::spawn(async move {
                    use tauri_plugin_shell::process::ProcessEvent;
                    while let Some(ev) = rx.recv().await {
                        match ev {
                            ProcessEvent::Stdout(line) | ProcessEvent::Stderr(line) => {
                                if let Some(ref mut f) = log.as_mut() {
                                    let _ = f.write_all(&line);
                                    let _ = writeln!(f);
                                }
                            }
                            _ => {}
                        }
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![sidecar_port])
        .run(tauri::generate_context!())
        .expect("error while running Ginno desktop shell");
}
