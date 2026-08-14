//! Atrium-style NSView hole-punch for the browser tile.
//!
//! The WKWebView that paints Chat / chrome sits on top of the window. Anywhere
//! the React tile is transparent, a sibling NSView *behind* the webview shows
//! through. Geometry arrives over `ginno:browser-tile`. Rust never sees Space
//! ownership. There is no `#[tauri::command]`.
//!
//! When `libginno_cef.dylib` + Helper.app sit next to the framework, the
//! hole gets a real CEF child (`cef_host.rs`). Until that host reports
//! ready, the tile stays an opaque Chrome screencast.

use std::sync::Mutex;

use tauri::{Manager, WebviewWindow};

use crate::{shell_log, BrowserTilePayload};

/// Native tile host. The wrapper NSView is retained by its superview; we only
/// keep a raw pointer so `with_webview` (which requires `Send`) can touch it
/// on the main thread.
pub struct BrowserTileHost {
    wrapper: Mutex<Option<usize>>,
}

impl BrowserTileHost {
    pub fn new() -> Self {
        Self {
            wrapper: Mutex::new(None),
        }
    }
}

/// Punch the WKWebView background and attach the sibling tile view.
pub fn prepare(window: &WebviewWindow) {
    #[cfg(not(target_os = "macos"))]
    {
        let _ = window;
    }
    #[cfg(target_os = "macos")]
    {
        use objc2::runtime::AnyObject;
        use objc2::{MainThreadMarker, MainThreadOnly};
        use objc2_app_kit::{NSView, NSWindowOrderingMode};
        use objc2_foundation::{NSNumber, NSPoint, NSRect, NSSize, NSString};
        use objc2_web_kit::WKWebView;

        let window_for_state = window.clone();
        let _ = window.with_webview(move |webview| unsafe {
            let Some(mtm) = MainThreadMarker::new() else {
                return;
            };
            let view: &WKWebView = &*webview.inner().cast::<WKWebView>();
            // Private KVC used by wry's own `transparent` feature and by atrium:
            // https://getatrium.dev/blog/embedding-real-browser-tauri
            let key = NSString::from_str("drawsBackground");
            let no = NSNumber::numberWithBool(false);
            let any = &*(view as *const WKWebView).cast::<AnyObject>();
            msg_send_set_value(any, &no, &key);

            let Some(content) = view.superview() else {
                return;
            };
            let wrapper = NSView::initWithFrame(
                NSView::alloc(mtm),
                NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(0.0, 0.0)),
            );
            wrapper.setWantsLayer(true);
            wrapper.setHidden(true);
            // Visual stack: wrapper sits *behind* the webview. Hit-testing still
            // follows subview order, so we hide the wrapper when the pane is
            // closed — otherwise chrome clicks would miss (atrium's surprise).
            let as_nsview = &*(view as *const WKWebView).cast::<NSView>();
            content.addSubview_positioned_relativeTo(
                &wrapper,
                NSWindowOrderingMode::Below,
                Some(as_nsview),
            );
            let ptr = RetainedExt::as_ptr(&wrapper) as usize;
            // Superview now owns the view; leak our extra retain so the pointer
            // stays valid for the life of the window.
            std::mem::forget(wrapper);
            if let Some(host) = window_for_state.try_state::<BrowserTileHost>() {
                if let Ok(mut guard) = host.wrapper.lock() {
                    *guard = Some(ptr);
                }
            }
            let app = window_for_state.app_handle().clone();
            let parent = ptr as *mut std::ffi::c_void;
            let wv_ptr = (view as *const WKWebView).cast::<std::ffi::c_void>() as *mut std::ffi::c_void;
            crate::cef_host::ensure_init(&app, parent);
            crate::cef_host::install_hittest(wv_ptr, parent);
        });
    }
}

/// Apply the last `ginno:browser-tile` payload to the native wrapper.
pub fn apply(app: &tauri::AppHandle, payload: &BrowserTilePayload) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (window, payload);
    }
    #[cfg(target_os = "macos")]
    {
        use objc2_app_kit::NSView;
        use objc2_foundation::{NSPoint, NSRect, NSSize};
        use objc2_web_kit::WKWebView;

        let Some(host) = app.try_state::<BrowserTileHost>() else {
            return;
        };
        let ptr = match host.wrapper.lock() {
            Ok(g) => *g,
            Err(_) => return,
        };
        let Some(ptr) = ptr else {
            // First tile event may race setup(); punch now and retry next frame.
            prepare(&window);
            return;
        };

        let visible =
            payload.visible.unwrap_or(true) && payload.width >= 80.0 && payload.height >= 80.0;
        let css_x = payload.x;
        let css_y = payload.y;
        let css_w = payload.width;
        let css_h = payload.height;
        let passthrough = payload.passthrough.unwrap_or(false);
        let app_for_cef = app.clone();
        let _ = window.with_webview(move |webview| unsafe {
            let wv: &WKWebView = &*webview.inner().cast::<WKWebView>();
            let wrapper = &*(ptr as *const NSView);
            if !visible {
                wrapper.setHidden(true);
                crate::cef_host::apply(
                    &app_for_cef,
                    ptr as *mut std::ffi::c_void,
                    (wv as *const WKWebView).cast::<std::ffi::c_void>() as *mut std::ffi::c_void,
                    false,
                    0,
                    0,
                    false,
                );
                return;
            }
            // WKWebView is flipped (top-left origin, like CSS) while its
            // superview is not — convert the tile's top-left corner instead
            // of reasoning about the flip by hand. The hole runs from the
            // superview's bottom edge (y=0, covering the native strip that
            // would otherwise show the window background) up to the tile's
            // top, leaving the pane chrome clickable.
            let bounds = wv.bounds();
            let parent = wv.superview();
            let corner =
                wv.convertPoint_toView(NSPoint::new(css_x, css_y), parent.as_deref());
            let in_superview = NSRect::new(
                NSPoint::new(corner.x, 0.0),
                NSSize::new(css_w, corner.y.max(0.0)),
            );
            wrapper.setFrame(in_superview);
            wrapper.setHidden(false);

            crate::shell_log(
                &app_for_cef,
                &format!(
                    "tile-apply payload={}x{} @{},{} wv={}x{} wv_frame=({:.0},{:.0},{:.0},{:.0}) sup=({:.0},{:.0}) wrapper=({:.0},{:.0},{:.0},{:.0})",
                    css_w as i32,
                    css_h as i32,
                    css_x as i32,
                    css_y as i32,
                    bounds.size.width as i32,
                    bounds.size.height as i32,
                    wv.frame().origin.x,
                    wv.frame().origin.y,
                    wv.frame().size.width,
                    wv.frame().size.height,
                    parent.as_ref().map(|p| p.bounds().size.width).unwrap_or(0.0),
                    parent.as_ref().map(|p| p.bounds().size.height).unwrap_or(0.0),
                    in_superview.origin.x,
                    in_superview.origin.y,
                    in_superview.size.width,
                    in_superview.size.height
                ),
            );

            crate::cef_host::apply(
                &app_for_cef,
                ptr as *mut std::ffi::c_void,
                (wv as *const WKWebView).cast::<std::ffi::c_void>() as *mut std::ffi::c_void,
                true,
                css_w as i32,
                css_h as i32,
                passthrough,
            );
        });
    }
}

/// Match the native window background to the web theme. Areas outside the
/// WKWebView (window strip below the webview) otherwise show the dark
/// default and read as a black bar in light theme.
pub fn set_window_background(app: &tauri::AppHandle, hex: &str) {
    #[cfg(target_os = "macos")]
    {
        let rgb = parse_hex_rgb(hex);
        let Some(window) = app.get_webview_window("main") else {
            return;
        };
        let inner_window = window.clone();
        let _ = window.run_on_main_thread(move || {
            let _ = inner_window.with_webview(move |webview| unsafe {
                use objc2_app_kit::{NSColor, NSView};
                use objc2_web_kit::WKWebView;
                let Some((r, g, b)) = rgb else {
                    return;
                };
                let wv: &WKWebView = &*webview.inner().cast::<WKWebView>();
                let Some(win) = wv.window() else {
                    return;
                };
                let color = NSColor::colorWithSRGBRed_green_blue_alpha(r, g, b, 1.0);
                win.setBackgroundColor(Some(&color));
            });
        });
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app, hex);
    }
}

#[cfg(target_os = "macos")]
fn parse_hex_rgb(hex: &str) -> Option<(f64, f64, f64)> {
    let h = hex.trim().trim_start_matches('#');
    if h.len() != 6 {
        return None;
    }
    let r = u8::from_str_radix(&h[0..2], 16).ok()?;
    let g = u8::from_str_radix(&h[2..4], 16).ok()?;
    let b = u8::from_str_radix(&h[4..6], 16).ok()?;
    Some((r as f64 / 255.0, g as f64 / 255.0, b as f64 / 255.0))
}

/// True when the packaged Frameworks payload is next to the .app executable.
pub fn framework_present(app: &tauri::AppHandle) -> bool {
    if let Ok(dir) = std::env::var("GINNO_CEF_DIR") {
        let p = std::path::PathBuf::from(dir);
        if p.join("Chromium Embedded Framework.framework").is_dir() {
            return true;
        }
        if p.ends_with("Chromium Embedded Framework.framework") && p.is_dir() {
            return true;
        }
    }
    // Packaged: Contents/Frameworks/Chromium Embedded Framework.framework
    if let Ok(exe) = std::env::current_exe() {
        if let Some(macos) = exe.parent() {
            let fw = macos
                .join("..")
                .join("Frameworks")
                .join("Chromium Embedded Framework.framework");
            if fw.is_dir() {
                return true;
            }
        }
    }
    // Dev / staged next to the crate (apps/desktop/Frameworks).
    if let Ok(res) = app.path().resource_dir() {
        let fw = res
            .join("..")
            .join("Frameworks")
            .join("Chromium Embedded Framework.framework");
        if fw.is_dir() {
            return true;
        }
    }
    false
}

pub fn log_host_ready(app: &tauri::AppHandle) {
    let present = framework_present(app);
    #[cfg(target_os = "macos")]
    crate::cef_host::ensure_init(app, std::ptr::null_mut());
    let live = {
        #[cfg(target_os = "macos")]
        {
            crate::cef_host::is_live()
        }
        #[cfg(not(target_os = "macos"))]
        {
            false
        }
    };
    shell_log(
        app,
        &format!("browser-tile host ready cef_framework={present} cef_live={live}"),
    );
}

#[cfg(target_os = "macos")]
unsafe fn msg_send_set_value(
    obj: &objc2::runtime::AnyObject,
    value: &objc2_foundation::NSNumber,
    key: &objc2_foundation::NSString,
) {
    use objc2_foundation::NSObjectNSKeyValueCoding;
    let nsobj: &objc2_foundation::NSObject =
        &*(obj as *const objc2::runtime::AnyObject).cast();
    let any_val = &*(value as *const objc2_foundation::NSNumber)
        .cast::<objc2::runtime::AnyObject>();
    nsobj.setValue_forKey(Some(any_val), key);
}

/// Tiny helper so we don't pull `objc2::rc::Retained` into the type of the
/// stored pointer. `Retained::as_ptr` is stable on objc2 0.6.
#[cfg(target_os = "macos")]
trait RetainedExt<T: ?Sized> {
    fn as_ptr(this: &Self) -> *const T;
}

#[cfg(target_os = "macos")]
impl<T: ?Sized> RetainedExt<T> for objc2::rc::Retained<T> {
    fn as_ptr(this: &Self) -> *const T {
        let r: &T = this;
        r as *const T
    }
}
