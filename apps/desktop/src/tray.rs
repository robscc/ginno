//! Menu-bar tray (decision Q4: tray icon + Dock stays). Left click toggles
//! the pin window; the menu offers explicit entries + quit. Between "Open
//! Main Window" and "Quit" sits the pinned-sessions quick-nav section:
//! every sidebar-pinned session (≤8, from /api/badge via badge.rs's 5s poll)
//! gets a menu item that shows+focuses the main window and makes the webview
//! switch to that session — same `__ginnoOpenSession` eval bridge as
//! notification clicks and ginno:// deep links, so the frontend needs zero
//! changes. badge.rs calls rebuild_menu whenever the pinned list changes
//! (pin/unpin/rename/title touch land within one 5s poll).

use tauri::menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;

use crate::badge::PinnedSession;
use crate::pin::toggle_pin;
use crate::shell_ui::{pick, shell_lang};

/// Menu-item id prefix for pinned-session entries; the session id follows
/// (ids are opaque strings — never split on inner separators, just strip).
const SESS_PREFIX: &str = "tray-session:";

/// Build the full tray menu for the current pinned list. Pure construction
/// (no tray lookup) so install_tray and rebuild_menu share one shape.
fn build_menu(app: &tauri::AppHandle, pinned: &[PinnedSession]) -> tauri::Result<Menu<tauri::Wry>> {
    let lang = shell_lang(app);
    let toggle = MenuItem::with_id(
        app,
        "tray-pin-toggle",
        pick(lang, "Show/Hide Floating Window", "显示/隐藏悬浮窗"),
        true,
        None::<&str>,
    )?;
    let open_main = MenuItem::with_id(
        app,
        "tray-open-main",
        pick(lang, "Open Main Window", "打开主窗口"),
        true,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(
        app,
        "tray-quit",
        pick(lang, "Quit Ginno", "退出 Ginno"),
        true,
        None::<&str>,
    )?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    // Pinned-sessions quick-nav: a disabled header (or the no-pins
    // placeholder — the section stays visible so users discover it) plus
    // one item per session. Empty titles get a bilingual "(untitled)".
    let header = if pinned.is_empty() {
        MenuItem::with_id(
            app,
            "tray-pinned-none",
            pick(lang, "No Pinned Sessions", "暂无置顶会话"),
            false,
            None::<&str>,
        )?
    } else {
        MenuItem::with_id(
            app,
            "tray-pinned-header",
            pick(lang, "Pinned Sessions", "置顶会话"),
            false,
            None::<&str>,
        )?
    };
    let mut items: Vec<Box<dyn IsMenuItem<tauri::Wry> + '_>> = vec![
        Box::new(toggle),
        Box::new(open_main),
        Box::new(sep1),
        Box::new(header),
    ];
    for s in pinned {
        let title = if s.title.trim().is_empty() {
            pick(lang, "(untitled)", "（未命名）").to_string()
        } else {
            s.title.clone()
        };
        items.push(Box::new(MenuItem::with_id(
            app,
            format!("{SESS_PREFIX}{}", s.id),
            title,
            true,
            None::<&str>,
        )?));
    }
    items.push(Box::new(sep2));
    items.push(Box::new(quit));
    let refs: Vec<&dyn IsMenuItem<tauri::Wry>> = items.iter().map(|b| b.as_ref()).collect();
    Menu::with_items(app, &refs)
}

/// Swap the live tray's menu for one built from `pinned` (called by the
/// badge watcher thread on pinned-list changes; tauri handles the main-
/// thread hop). No tray yet (install failed) → silent no-op like the
/// running-title apply.
pub(crate) fn rebuild_menu(app: &tauri::AppHandle, pinned: &[PinnedSession]) -> tauri::Result<()> {
    let menu = build_menu(app, pinned)?;
    if let Some(tray) = app.tray_by_id("ginno-tray") {
        tray.set_menu(Some(menu))?;
    }
    Ok(())
}

pub(crate) fn install_tray(app: &tauri::App) -> tauri::Result<()> {
    // Empty pinned list at install time — the sidecar isn't up yet; the
    // first badge poll (≤5s after it is) rebuilds with real data.
    let menu = build_menu(app.handle(), &[])?;
    let mut tray = TrayIconBuilder::with_id("ginno-tray")
        .tooltip("Ginno")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            if let Some(sid) = id.strip_prefix(SESS_PREFIX) {
                // Same path as notification clicks: restore+focus main, then
                // eval __ginnoOpenSession(sid) (registered by AppShell; the
                // guarded eval no-ops on a still-booting webview).
                if !sid.is_empty() {
                    crate::notify::focus_and_open(app, "session", sid);
                }
                return;
            }
            match id {
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
            }
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
