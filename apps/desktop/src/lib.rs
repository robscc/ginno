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
//!   7. Serve the mods broker in-process (design §2 桌面形态): the shell owns
//!      the broker's Unix socket + token file (app data dir) and injects
//!      GINNO_MOD_BROKER_SOCK / GINNO_MOD_BROKER_TOKEN / GINNO_MOD_RUNNER_PATH
//!      into the sidecar env; if the broker fails to come up the sidecar gets
//!      no env and falls back to its dev-spawn / discovery ladder.
//!
//! In dev (`tauri dev`), the user runs `pnpm dev:runtime` in a separate
//! terminal; this file only spawns the runtime in release builds.

mod code_cmds;
mod common;
mod keep_awake;
mod badge;
mod menu;
mod notify;
mod pin;
mod shell_ui;
mod sidecar;
mod tray;

#[cfg(not(debug_assertions))]
use std::net::{SocketAddr, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{DragDropEvent, Listener, Manager, WindowEvent};
use tauri_plugin_global_shortcut::ShortcutState;

use crate::common::{ginno_home_path, shell_log};
use crate::keep_awake::KeepAwake;
use crate::menu::{install_debug_menu, open_in_editor, reveal_path};
use crate::notify::{show_notification_and_wait, NotifyPayload};
use crate::pin::{
    capture_pin_geo, floating_setting_bool, load_floating_geo, load_pin_prefs, persist_pin_geo,
    register_pin_hotkey, summon_pin, toggle_pin_now, PinState, PIN_LABEL,
};
use crate::sidecar::{restart_sidecar, start_mod_broker, ModBrokerState, RestartLock, RuntimeProcess};
#[cfg(not(debug_assertions))]
use crate::sidecar::{
    kill_stale_sidecar, sidecar_listening, sidecar_port, spawn_sidecar, wait_for_sidecar,
};
#[cfg(not(debug_assertions))]
use crate::shell_ui::{base64, localize_shell_html, shell_lang, ERROR_HTML, SPLASH_HTML};
use crate::tray::install_tray;

pub fn run() {
    // Set while a real quit is in flight (⌘Q / menu / ExitRequested) so the
    // CloseRequested handler below destroys the window instead of hiding it.
    let quitting = Arc::new(AtomicBool::new(false));

    let app = tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    // One app-wide hotkey (the pin summon); ignore key-release.
                    // Summon, not plain toggle: the hotkey must end with the
                    // composer on screen and focused (see summon_pin_now).
                    if event.state() == ShortcutState::Pressed {
                        summon_pin(app);
                    }
                })
                .build(),
        )
        // Code panel: Finder reveal / open-with-default-app (design §4.4).
        //
        // Registered, but deliberately WITHOUT its JS link-interception script
        // and WITHOUT any `opener:*` permission for the webview:
        //   * the panel reaches the opener only through `code_reveal` /
        //     `code_open_external`, which do their own containment check — so
        //     there is no reason to also let the page call the plugin command
        //     directly and skip that check;
        //   * the injected script would re-route the app's existing
        //     `window.open(url, "_blank")` calls (ChatStream, runtime.ts),
        //     i.e. change behaviour that already works today.
        .plugin(
            tauri_plugin_opener::Builder::new()
                .open_js_links_on_click(false)
                .build(),
        )
        // Home mount picker (docs/home-mount-picker-design.md §2.2): the mount
        // panel's Browse… button opens the native directory picker from the
        // webview. Registered with the default builder; the webview-side grant
        // is deliberately narrow — capabilities/default.json carries only
        // `dialog:allow-open`, so the page can pick directories but cannot
        // spawn ask/message/confirm dialogs.
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            pin::pin_toggle,
            pin::pin_set_mode,
            pin::pin_get_mode,
            pin::pin_hide,
            pin::pin_open_main,
            pin::pin_apply_prefs,
            pin::pin_hotkey_status,
            code_cmds::code_reveal,
            code_cmds::code_open_external,
            code_cmds::code_trash,
            keep_awake::set_keep_awake,
            keep_awake::get_keep_awake
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
                                format!("http://127.0.0.1:{}/", sidecar_port()).parse()
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
            // 双实例防呆：只改端口不换数据目录的话，两个 sidecar 会并发双写
            // 同一份 sessions/checkpoint —— 比端口冲突更糟，启动时就点名。
            if std::env::var_os("GINNO_RUNTIME_PORT").is_some()
                && std::env::var_os("GINNO_HOME").is_none()
            {
                shell_log(
                    app,
                    "GINNO_RUNTIME_PORT is set but GINNO_HOME is not — this instance will \
                     share ~/.ginno with the default instance; pair the two envs to run \
                     side-by-side safely",
                );
            }
            app.manage(RuntimeProcess(Mutex::new(None)));
            app.manage(RestartLock(Mutex::new(())));
            app.manage(KeepAwake(Mutex::new(None)));

            // In-process mods broker (design §2 桌面形态), started before the
            // sidecar so its env is meaningful at spawn. Down = no env → the
            // runtime falls back to dev-spawn / ~/.ginno/bin discovery.
            let (broker_sock, broker_token) = match start_mod_broker(app) {
                Some((sock, token)) => (Some(sock), Some(token)),
                None => {
                    shell_log(app, "mods broker did not come up — runtime falls back to dev-spawn/discovery");
                    (None, None)
                }
            };
            // Bundled runner (release): staged by `make sidecar` next to the
            // runtime bundle. Dev builds don't package resources; the None
            // lets the runtime find packages/mod-runner/dist/ itself.
            let broker_runner = app
                .path()
                .resource_dir()
                .ok()
                .map(|r| r.join("resources").join("mod-runner.mjs"))
                .filter(|p| p.is_file());
            app.manage(ModBrokerState {
                sock: broker_sock,
                token: broker_token,
                runner: broker_runner,
            });

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
            // Dock badge: needs = unread sessions + unseen schedule runs +
            // team blocked/done (agent-team §5.3.1). Self-throttling poller;
            // no-ops while the sidecar is down.
            badge::spawn_badge_watcher(app.handle().clone());
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

                let addr: SocketAddr = ([127, 0, 0, 1], sidecar_port()).into();
                let ready_now = TcpStream::connect_timeout(&addr, Duration::from_millis(150)).is_ok();

                if !ready_now {
                    if let Some(window) = app.get_webview_window("main") {
                        let html = localize_shell_html(SPLASH_HTML, shell_lang(app))
                            .replace("__PORT__", &sidecar_port().to_string());
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
                    let addr: SocketAddr = ([127, 0, 0, 1], sidecar_port()).into();
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
                        let html = localize_shell_html(ERROR_HTML, shell_lang(&handle))
                            .replace("__PORT__", &sidecar_port().to_string());
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
                    let url = tauri::Url::parse(&format!("http://127.0.0.1:{}/", sidecar_port()))
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
            // Vibe Island 卡片点击 → macOS 按 manifest 的 jumpRule 打开
            // ginno://session/<id>(scheme 注册在 Info.plist,tauri-bundler
            // 自动合并)。RunEvent::Opened 只在 macOS/iOS/Android 上存在。
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Opened { urls } => {
                for url in urls {
                    // 只认 ginno://session/<sid>;其余 URL 静默忽略。
                    if url.scheme() != "ginno" || url.host_str() != Some("session") {
                        shell_log(app_handle, &format!("deep_link ignored url={url}"));
                        continue;
                    }
                    // sid 只允许 [A-Za-z0-9_-](会话 id 是 uuid hex;截断任何
                    // 多余字符,防 query/fragment 混入 JS eval)。
                    let sid: String = url
                        .path()
                        .trim_start_matches('/')
                        .chars()
                        .take_while(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
                        .collect();
                    if sid.is_empty() {
                        shell_log(app_handle, &format!("deep_link empty sid url={url}"));
                        continue;
                    }
                    shell_log(app_handle, &format!("deep_link session={sid}"));
                    crate::notify::deep_link_open_session(app_handle, &sid);
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
                // keep-awake 的 caffeinate 本会随父退出，这里显式回收干净些。
                if let Some(state) = app_handle.try_state::<KeepAwake>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
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
