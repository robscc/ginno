//! Shared shell helpers: `~/.ginno` home resolution and the best-effort
//! `shell.log` used by every subsystem.

pub(crate) fn ginno_home_path<M: tauri::Manager<tauri::Wry>>(app: &M) -> std::path::PathBuf {
    if let Ok(p) = std::env::var("GINNO_HOME") {
        return std::path::PathBuf::from(p);
    }
    app.path()
        .home_dir()
        .map(|h| h.join(".ginno"))
        .unwrap_or_else(|_| std::path::PathBuf::from(".ginno"))
}

/// Append one line to ~/.ginno/logs/shell.log (same convention as sidecar.log).
/// Best-effort diagnostics for the notification / window-visibility flow.
pub(crate) fn shell_log<M: tauri::Manager<tauri::Wry>>(app: &M, line: &str) {
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

/// Dock 图标只跟随主窗（macOS）：主窗隐藏即切 Accessory（Ginno 只活在
/// 小齿轮 topbar tray 里，Dock 图标消失——「最小化到 topbar」），主窗
/// 可见时切回 Regular。悬浮窗（pin）是覆盖式 summon UI，**不**参与判定
/// ——Accessory 下 mini 的输入框照常可用（Spotlight 同款），否则唤出
/// 悬浮窗会把 Dock 图标拽回来，用户会看到「应用明明关了 Dock 还有它」。
/// 在窗口 show/hide 的每个转捩点调用；accessory 应用收不到 Dock Reopen
/// 事件，tray 菜单/通知/深链是恢复主窗的入口（预期行为）。
#[cfg(target_os = "macos")]
pub(crate) fn sync_dock_policy(app: &tauri::AppHandle) {
    use tauri::Manager;

    let main_visible = app
        .get_webview_window("main")
        .map(|w| w.is_visible().unwrap_or(false))
        .unwrap_or(false);
    let policy = if main_visible {
        tauri::ActivationPolicy::Regular
    } else {
        tauri::ActivationPolicy::Accessory
    };
    let name = if main_visible { "Regular" } else { "Accessory" };
    match app.set_activation_policy(policy) {
        Ok(()) => shell_log(app, &format!("dock policy -> {name}")),
        Err(e) => shell_log(app, &format!("set_activation_policy FAILED: {e}")),
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn sync_dock_policy(_app: &tauri::AppHandle) {}
