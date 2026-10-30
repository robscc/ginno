//! Menu-bar tray (decision Q4: tray icon + Dock stays). Left click toggles
//! the pin window; the menu offers explicit entries + quit.

use tauri::menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;

use crate::pin::toggle_pin;
use crate::shell_ui::{pick, shell_lang};

pub(crate) fn install_tray(app: &tauri::App) -> tauri::Result<()> {
    let lang = shell_lang(app);
    let toggle = MenuItem::with_id(app, "tray-pin-toggle", pick(lang, "Show/Hide Floating Window", "显示/隐藏悬浮窗"), true, None::<&str>)?;
    let open_main = MenuItem::with_id(app, "tray-open-main", pick(lang, "Open Main Window", "打开主窗口"), true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray-quit", pick(lang, "Quit Ginno", "退出 Ginno"), true, None::<&str>)?;
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
                    // Accessory → Regular：主窗回来了，Dock 图标也回来。
                    crate::common::sync_dock_policy(app);
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
