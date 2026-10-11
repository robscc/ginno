//! Floating quick-chat window ("pin") — docs/floating-window-design.md §2/§4.
//! An always-on-top, undecorated second webview with two shapes (mini ⇄
//! pill); Rust owns mode/geometry and the global hotkey.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};

use crate::common::shell_log;
use crate::notify::focus_and_open;
use crate::shell_ui::{pick, shell_lang};

pub(crate) const PIN_LABEL: &str = "pin";
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
pub(crate) struct FloatingGeo {
    #[serde(default)]
    mini: Option<Geo>,
    #[serde(default)]
    pill: Option<PillGeo>,
}

/// Mirror of the `floating` key in settings.json (subset the shell acts on).
/// Pushed by the web UI via `pin_apply_prefs`; also read once at startup so
/// the hotkey works before any webview is up.
#[derive(serde::Serialize, serde::Deserialize, Clone)]
pub(crate) struct PinPrefs {
    #[serde(default = "default_hotkey")]
    pub(crate) hotkey: String,
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

pub(crate) struct PinState {
    /// "mini" | "pill" — Rust is the source of truth; webview mirrors via
    /// the `pin:mode` event / `pin_get_mode`.
    pub(crate) mode: Mutex<String>,
    pub(crate) geo: Mutex<FloatingGeo>,
    pub(crate) geo_path: std::path::PathBuf,
    pub(crate) prefs: Mutex<PinPrefs>,
    /// Whether the CURRENT prefs hotkey is actually registered with the OS.
    /// False = taken by another app / unparsable; tray menu still works.
    /// Surfaced to Settings → 悬浮窗 via `pin_hotkey_status`.
    pub(crate) hotkey_active: AtomicBool,
}

pub(crate) fn load_floating_geo(home: &Path) -> FloatingGeo {
    std::fs::read_to_string(home.join("floating.json"))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

/// Read the `floating` prefs from settings.json, tolerating a missing key
/// (existing users' settings.json predates it and PUT only rewrites on save).
pub(crate) fn load_pin_prefs(home: &Path) -> PinPrefs {
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

pub(crate) fn floating_setting_bool(home: &Path, key: &str, default: bool) -> bool {
    let Ok(text) = std::fs::read_to_string(home.join("settings.json")) else {
        return default;
    };
    serde_json::from_str::<serde_json::Value>(&text)
        .ok()
        .and_then(|v| v.get("floating").and_then(|f| f.get(key)).and_then(|x| x.as_bool()))
        .unwrap_or(default)
}

pub(crate) fn persist_pin_geo(app: &tauri::AppHandle) {
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
pub(crate) fn capture_pin_geo(w: &WebviewWindow, st: &PinState) {
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
        .title(pick(shell_lang(app), "Ginno Floating Window", "Ginno 悬浮窗"))
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

/// Shared hide branch: bank the outgoing geometry, hide, persist.
fn hide_pin_now(app: &tauri::AppHandle, w: &WebviewWindow) {
    if let Some(st) = app.try_state::<PinState>() {
        capture_pin_geo(w, &st);
    }
    let _ = w.hide();
    persist_pin_geo(app);
    shell_log(app, "pin: hide");
}

/// Show in the last-used shape (tray / show-at-launch / Settings test button).
pub(crate) fn toggle_pin_now(app: &tauri::AppHandle) {
    let w = match ensure_pin_window(app) {
        Ok(w) => w,
        Err(e) => {
            shell_log(app, &format!("ensure_pin_window FAILED: {e}"));
            return;
        }
    };
    if w.is_visible().unwrap_or(false) {
        hide_pin_now(app, &w);
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
            // set_focus only makes the WINDOW key — WKWebView's DOM focus stays
            // wherever it last was, and the composer's autoFocus ran at first
            // mount (the webview survives hide/show). Tell the webview to put
            // the caret in the input so a summoned mini is immediately typeable.
            let _ = w.emit("pin:focus-input", ());
        }
        shell_log(app, &format!("pin: show mode={mode}"));
    }
}

/// Global-hotkey semantics — "summon the composer":
///   hidden (any last shape) → show as mini; visible pill → expand to mini;
///   visible mini → hide (toggle off).
/// The hotkey's contract is that the user can TYPE right after it fires —
/// a click-through pill is a pure status light that cannot be clicked, so
/// the hotkey is its only expansion path (Settings → 悬浮窗 promises
/// 「需用快捷键唤出」). Hence never land on a shape without an input box.
fn summon_pin_now(app: &tauri::AppHandle) {
    let w = match ensure_pin_window(app) {
        Ok(w) => w,
        Err(e) => {
            shell_log(app, &format!("ensure_pin_window FAILED: {e}"));
            return;
        }
    };
    let mode = app
        .try_state::<PinState>()
        .map(|s| s.mode.lock().unwrap().clone())
        .unwrap_or_else(|| "mini".to_string());
    if w.is_visible().unwrap_or(false) && mode == "mini" {
        hide_pin_now(app, &w);
        return;
    }
    if mode != "mini" {
        // Banks the pill geometry, applies the mini shape and notifies the
        // webview (pin:mode → PinStream remounts with autoFocus).
        set_pin_mode(app, "mini");
    } else {
        apply_pin_mode_geometry(app, &w);
        apply_pin_window_prefs(app, &w);
    }
    let _ = w.show();
    let _ = w.set_focus();
    let _ = w.emit("pin:focus-input", ());
    shell_log(app, "pin: summon mode=mini");
}

/// Run `now` on the main thread. Release builds serve /pin from the sidecar;
/// before it is up the window would paint a connection-refused page, so defer
/// until the port accepts.
fn pin_invoke_deferred(app: &tauri::AppHandle, now: fn(&tauri::AppHandle)) {
    #[cfg(not(debug_assertions))]
    if !crate::sidecar::sidecar_listening() {
        shell_log(app, "pin toggle deferred: sidecar not up yet");
        let h = app.clone();
        std::thread::spawn(move || {
            if crate::sidecar::wait_for_sidecar(std::time::Duration::from_secs(90)) {
                let h2 = h.clone();
                let _ = h.run_on_main_thread(move || now(&h2));
            }
        });
        return;
    }
    now(app);
}

pub(crate) fn toggle_pin(app: &tauri::AppHandle) {
    pin_invoke_deferred(app, toggle_pin_now);
}

pub(crate) fn summon_pin(app: &tauri::AppHandle) {
    pin_invoke_deferred(app, summon_pin_now);
}

/// Register `hotkey`, recording the outcome in PinState::hotkey_active.
/// Returns true when the shortcut is now live with the OS.
pub(crate) fn register_pin_hotkey(app: &tauri::AppHandle, hotkey: &str) -> bool {
    let ok = match hotkey.parse::<Shortcut>() {
        Ok(sc) => match app.global_shortcut().register(sc) {
            Ok(()) => {
                shell_log(app, &format!("pin hotkey registered: {hotkey}"));
                true
            }
            Err(e) => {
                shell_log(
                    app,
                    &format!("pin hotkey register FAILED: {e} (tray menu still works; change it in Settings → Floating Window)"),
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
pub(crate) fn pin_toggle(app: tauri::AppHandle) {
    toggle_pin(&app);
}

#[tauri::command]
pub(crate) fn pin_set_mode(app: tauri::AppHandle, mode: String) {
    set_pin_mode(&app, &mode);
}

#[tauri::command]
pub(crate) fn pin_get_mode(app: tauri::AppHandle) -> String {
    app.try_state::<PinState>()
        .map(|s| s.mode.lock().unwrap().clone())
        .unwrap_or_else(|| "mini".to_string())
}

#[tauri::command]
pub(crate) fn pin_hide(app: tauri::AppHandle) {
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
pub(crate) fn pin_open_main(app: tauri::AppHandle, session_id: Option<String>) {
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
pub(crate) fn pin_apply_prefs(app: tauri::AppHandle, prefs: PinPrefs) -> bool {
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
pub(crate) fn pin_hotkey_status(app: tauri::AppHandle) -> bool {
    app.try_state::<PinState>()
        .map(|st| st.hotkey_active.load(Ordering::Relaxed))
        .unwrap_or(false)
}
