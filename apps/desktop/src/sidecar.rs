//! Sidecar (Python runtime) process management: spawn / wait / restart /
//! stale-port reclaim, plus the in-process mods broker endpoint handed to the
//! sidecar via env at spawn.

use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

use crate::common::{ginno_home_path, shell_log};

pub(crate) const SIDECAR_PORT: u16 = 8787;

/// Handle to the spawned runtime process, so `RunEvent::Exit` / Debug → Restart
/// can terminate it. Present in both release (bundled ginno-runtime) and dev
/// (`uv run uvicorn`) so the menu can restart the backend without quitting.
pub(crate) struct RuntimeProcess(pub(crate) Mutex<Option<Child>>);

/// Serialize Debug → Restart Runtime so a double-click cannot spawn two sidecars.
pub(crate) struct RestartLock(pub(crate) Mutex<()>);

fn open_log_file_for<M: tauri::Manager<tauri::Wry>>(app: &M) -> Option<std::fs::File> {
    let logs = ginno_home_path(app).join("logs");
    std::fs::create_dir_all(&logs).ok()?;
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("sidecar.log"))
        .ok()
}

pub(crate) fn sidecar_listening() -> bool {
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
pub(crate) fn kill_stale_sidecar() {
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

// ---------------------------------------------------------------------------
// In-process mods broker — design §2 (桌面形态): one crate, two hosts. The
// desktop shell serves the broker's Unix socket from inside this process
// (dev/web spawns the same crate's CLI instead) and injects the endpoint into
// the sidecar's env; the runtime's ModChannel then connects here instead of
// spawning its own broker (bridge_utils.py::ensure_broker). The broker
// outlives sidecar restarts on purpose (design 目标 4: runtime restart never
// touches the broker or the runners).
// ---------------------------------------------------------------------------

/// Endpoint facts handed to the sidecar via env at spawn. Everything `None`
/// means the broker never came up: the sidecar gets no mods env and falls
/// back to its existing dev-spawn / repo-target / ~/.ginno/bin discovery
/// ladder (bridge_utils.py) — the documented degradation path.
#[derive(Default)]
pub(crate) struct ModBrokerState {
    /// Unix socket path (GINNO_MOD_BROKER_SOCK).
    pub(crate) sock: Option<PathBuf>,
    /// The broker's sibling `<sock>.token` file, 0600 (GINNO_MOD_BROKER_TOKEN
    /// — Python reads the file, the token never travels as a value).
    pub(crate) token: Option<PathBuf>,
    /// mod-runner.mjs staged inside the app bundle's Resources
    /// (GINNO_MOD_RUNNER_PATH). None in dev builds: the runtime's repo-dist
    /// ladder finds packages/mod-runner/dist/mod-runner.mjs itself.
    pub(crate) runner: Option<PathBuf>,
}

/// Start the broker on Tauri's async runtime and wait (bounded) for its
/// socket + token to appear. Failure is non-fatal: mods simply run on the
/// runtime's fallback ladder (see [`ModBrokerState`]).
pub(crate) fn start_mod_broker<M: tauri::Manager<tauri::Wry>>(app: &M) -> Option<(PathBuf, PathBuf)> {
    let dir = app.path().app_local_data_dir().ok()?;
    if let Err(e) = std::fs::create_dir_all(&dir) {
        shell_log(app, &format!("mods broker: create {}: {e}", dir.display()));
        return None;
    }
    let sock = dir.join("ginno-mod-broker.sock");
    // Sibling token file, exactly the naming runtime.rs::token_path_of uses
    // (the CLI's `<socket>.token` convention, design §8.6).
    let token = dir.join("ginno-mod-broker.sock.token");
    let spawned = sock.clone();
    tauri::async_runtime::spawn(async move {
        // The store path rides the config push from Python (the broker holds
        // no on-disk config — same None as the CLI's standalone serve).
        let broker = ginno_mod_broker::Broker::new(None);
        if let Err(e) = ginno_mod_broker::runtime::serve(broker, spawned).await {
            eprintln!("[ginno-desktop] mods broker exited: {e}");
        }
    });
    // serve() binds within milliseconds, but stay bounded: if neither file
    // shows up in 5s, treat the broker as down and fall back.
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while std::time::Instant::now() < deadline {
        if sock.exists() && token.exists() {
            return Some((sock, token));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    None
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

pub(crate) fn spawn_sidecar<M: tauri::Manager<tauri::Wry>>(app: &M) -> Result<Child, String> {
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
    // Mods broker endpoint (design §2.1 step 2): socket + token-file env so
    // the runtime's ModChannel connects to the in-shell broker, plus the
    // bundled runner path. Absent entries simply don't get set — the runtime
    // then walks its dev-spawn / discovery ladder as before.
    if let Some(state) = app.try_state::<ModBrokerState>() {
        if let (Some(sock), Some(token)) = (&state.sock, &state.token) {
            cmd.env("GINNO_MOD_BROKER_SOCK", sock);
            cmd.env("GINNO_MOD_BROKER_TOKEN", token);
        }
        if let Some(runner) = &state.runner {
            cmd.env("GINNO_MOD_RUNNER_PATH", runner);
        }
    }
    if let Some(log) = open_log_file_for(app) {
        let log_err = log
            .try_clone()
            .map_err(|e| format!("clone log handle: {e}"))?;
        cmd.stdout(Stdio::from(log)).stderr(Stdio::from(log_err));
    }
    cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))
}

pub(crate) fn wait_for_sidecar(timeout: Duration) -> bool {
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
pub(crate) fn restart_sidecar(app: &tauri::AppHandle) {
    shell_log(app, "debug: restart runtime requested");
    // Splash first so the webview is not sitting on a dying origin.
    #[cfg(not(debug_assertions))]
    if let Some(window) = app.get_webview_window("main") {
        let html = crate::shell_ui::localize_shell_html(crate::shell_ui::SPLASH_HTML, crate::shell_ui::shell_lang(app))
            .replace("__PORT__", &SIDECAR_PORT.to_string());
        let url = format!("data:text/html;base64,{}", crate::shell_ui::base64(html.as_bytes()));
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
            let html = crate::shell_ui::localize_shell_html(crate::shell_ui::ERROR_HTML, crate::shell_ui::shell_lang(app))
                .replace("__PORT__", &SIDECAR_PORT.to_string());
            let err_url = format!("data:text/html;base64,{}", crate::shell_ui::base64(html.as_bytes()));
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
