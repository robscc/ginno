//! App menu: the Debug submenu (restart the sidecar, open logs, reload the
//! UI) plus the Finder / editor open helpers it and the menu handler share.

use std::process::Command;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};

use crate::shell_ui::{pick, shell_lang};

/// Open a path in Finder (macOS) / Explorer / xdg-open. Best-effort.
pub(crate) fn reveal_path(path: &std::path::Path) {
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

pub(crate) fn open_in_editor(path: &std::path::Path) {
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

pub(crate) fn install_debug_menu(app: &tauri::App) -> tauri::Result<()> {
    let lang = shell_lang(app);
    let restart = MenuItem::with_id(
        app,
        "debug-restart-runtime",
        pick(lang, "Restart Backend", "重启后端"),
        true,
        Some("CmdOrCtrl+Alt+R"),
    )?;
    let sidecar_log = MenuItem::with_id(app, "debug-open-sidecar-log", pick(lang, "Open Sidecar Log", "打开 Sidecar 日志"), true, None::<&str>)?;
    let shell_log_item = MenuItem::with_id(app, "debug-open-shell-log", pick(lang, "Open Shell Log", "打开 Shell 日志"), true, None::<&str>)?;
    let logs_dir = MenuItem::with_id(app, "debug-reveal-logs", pick(lang, "Reveal Logs in Finder", "在 Finder 中打开日志目录"), true, None::<&str>)?;
    let home_dir = MenuItem::with_id(app, "debug-reveal-home", pick(lang, "Reveal ~/.ginno in Finder", "在 Finder 中打开 ~/.ginno"), true, None::<&str>)?;
    let reload = MenuItem::with_id(app, "debug-reload-ui", pick(lang, "Reload UI", "重新加载界面"), true, Some("CmdOrCtrl+R"))?;

    let debug = Submenu::with_id_and_items(
        app,
        "debug",
        pick(lang, "Debug", "调试"),
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
