//! Optional CEF host: `dlopen` `libginno_cef.dylib` and attach a real
//! Chromium view as a child of the atrium hole.
//!
//! Missing dylib / helpers / framework → no-op. The sidecar stays on
//! Chrome screencast. There is no `#[tauri::command]`. Space / ownership
//! never enter this file; the only extra bit on the tile event is
//! `passthrough` (whether the WKWebView should forward hits into the hole).

use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::Manager;

use crate::shell_log;

const RTLD_LAZY: c_int = 1;
const RTLD_NOW: c_int = 2;
const RTLD_GLOBAL: c_int = 8;

extern "C" {
    fn dlopen(filename: *const c_char, flags: c_int) -> *mut c_void;
    fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    fn dlerror() -> *const c_char;
}

type InitFn = unsafe extern "C" fn(
    *const c_char,
    *const c_char,
    *const c_char,
    *const c_char,
    c_int,
    *mut c_void,
) -> c_int;
type AttachFn = unsafe extern "C" fn(*mut c_void, c_int, c_int) -> c_int;
type SetHiddenFn = unsafe extern "C" fn(c_int);
type InstallHitFn = unsafe extern "C" fn(*mut c_void, *mut c_void);
type SetPassFn = unsafe extern "C" fn(c_int);
type ReadyFn = unsafe extern "C" fn() -> c_int;
type PortFn = unsafe extern "C" fn() -> c_int;
type ErrFn = unsafe extern "C" fn() -> *const c_char;

struct Api {
    init: InitFn,
    attach: AttachFn,
    set_hidden: SetHiddenFn,
    install_hit: InstallHitFn,
    set_pass: SetPassFn,
    ready: ReadyFn,
    port: PortFn,
    last_error: ErrFn,
}

struct State {
    api: Option<Api>,
    inited: bool,
    load_attempted: bool,
}

static STATE: Mutex<State> = Mutex::new(State {
    api: None,
    inited: false,
    load_attempted: false,
});

static LIVE: AtomicBool = AtomicBool::new(false);

fn dlerr() -> String {
    unsafe {
        let p = dlerror();
        if p.is_null() {
            return "unknown dlerror".into();
        }
        CStr::from_ptr(p).to_string_lossy().into_owned()
    }
}

fn lookup<T>(handle: *mut c_void, name: &str) -> Result<T, String> {
    let c = CString::new(name).map_err(|_| "bad symbol".to_string())?;
    unsafe {
        let p = dlsym(handle, c.as_ptr());
        if p.is_null() {
            return Err(format!("dlsym {name}: {}", dlerr()));
        }
        Ok(std::mem::transmute_copy(&p))
    }
}

fn frameworks_dir(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("GINNO_CEF_DIR") {
        let p = PathBuf::from(dir);
        if p.join("Chromium Embedded Framework.framework").is_dir() {
            return Some(p);
        }
        if p.ends_with("Chromium Embedded Framework.framework") && p.is_dir() {
            return p.parent().map(|x| x.to_path_buf());
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(macos) = exe.parent() {
            let fw = macos.join("..").join("Frameworks");
            if fw.join("Chromium Embedded Framework.framework").is_dir() {
                return Some(fw);
            }
        }
    }
    if let Ok(res) = app.path().resource_dir() {
        let fw = res.join("..").join("Frameworks");
        if fw.join("Chromium Embedded Framework.framework").is_dir() {
            return Some(fw);
        }
    }
    None
}

fn helper_exe(fw: &Path) -> Option<PathBuf> {
    let p = fw
        .join("Ginno Helper.app")
        .join("Contents")
        .join("MacOS")
        .join("Ginno Helper");
    p.is_file().then_some(p)
}

fn dylib_path(fw: &Path) -> Option<PathBuf> {
    let p = fw.join("libginno_cef.dylib");
    p.is_file().then_some(p)
}

fn main_bundle() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    // .../Ginno.app/Contents/MacOS/Ginno → Ginno.app
    exe.parent()?.parent()?.parent().map(|p| p.to_path_buf())
}

fn ginno_home(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(p) = std::env::var("GINNO_HOME") {
        return PathBuf::from(p);
    }
    match app.path().home_dir() {
        Ok(h) => h.join(".ginno"),
        Err(_) => PathBuf::from("/tmp/.ginno"),
    }
}

fn cache_path(app: &tauri::AppHandle) -> PathBuf {
    // Same profile the Chrome fallback uses so imported cookies survive
    // the engine swap. ginno_cef_init evicts leftover Chrome holding
    // this dir before cef_initialize — a second profile would drop
    // imported cookies.
    ginno_home(app).join("browser").join("profile")
}

fn write_status(app: &tauri::AppHandle, port: i32, ready: bool, err: &str) {
    let dir = ginno_home(app).join("browser");
    let _ = std::fs::create_dir_all(&dir);
    let body = serde_json::json!({
        "port": port,
        "ready": ready,
        "pid": std::process::id(),
        "error": err,
    });
    let _ = std::fs::write(dir.join("cef-cdp.json"), body.to_string());
}

fn free_port() -> i32 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .ok()
        .and_then(|s| s.local_addr().ok())
        .map(|a| a.port() as i32)
        .unwrap_or(9333)
}

fn is_packaged_app() -> bool {
    main_bundle()
        .and_then(|p| p.extension().map(|e| e == "app"))
        .unwrap_or(false)
}

fn load_api(app: &tauri::AppHandle) -> Result<Api, String> {
    if !is_packaged_app() && std::env::var("GINNO_CEF_FORCE").is_err() {
        return Err("CEF host only starts inside Ginno.app".into());
    }
    let fw = frameworks_dir(app).ok_or("CEF Frameworks dir not found")?;
    let dy = dylib_path(&fw).ok_or("libginno_cef.dylib not staged")?;
    if helper_exe(&fw).is_none() {
        return Err("Ginno Helper.app not staged".into());
    }
    // Load the framework first (RTLD_GLOBAL) so libginno_cef.dylib's
    // dynamically-looked-up CEF symbols resolve. The dylib itself is
    // opened LAZY because it was compiled with -undefined dynamic_lookup.
    let fw_bin = fw
        .join("Chromium Embedded Framework.framework")
        .join("Chromium Embedded Framework");
    let fw_c = CString::new(fw_bin.to_string_lossy().as_bytes()).map_err(|_| "fw path")?;
    unsafe {
        let fw_h = dlopen(fw_c.as_ptr(), RTLD_NOW | RTLD_GLOBAL);
        if fw_h.is_null() {
            return Err(format!("dlopen {}: {}", fw_bin.display(), dlerr()));
        }
    }
    let path = CString::new(dy.to_string_lossy().as_bytes()).map_err(|_| "dylib path")?;
    unsafe {
        let handle = dlopen(path.as_ptr(), RTLD_LAZY);
        if handle.is_null() {
            return Err(format!("dlopen {}: {}", dy.display(), dlerr()));
        }
        Ok(Api {
            init: lookup(handle, "ginno_cef_init")?,
            attach: lookup(handle, "ginno_cef_attach")?,
            set_hidden: lookup(handle, "ginno_cef_set_hidden")?,
            install_hit: lookup(handle, "ginno_cef_install_hittest")?,
            set_pass: lookup(handle, "ginno_cef_set_passthrough")?,
            ready: lookup(handle, "ginno_cef_ready")?,
            port: lookup(handle, "ginno_cef_debug_port")?,
            last_error: lookup(handle, "ginno_cef_last_error")?,
        })
    }
}

fn c_path(p: &Path) -> Result<CString, String> {
    CString::new(p.to_string_lossy().as_bytes()).map_err(|_| "path with NUL".into())
}

/// Load the dylib (once) and cef_initialize (once). Safe to call from
/// `prepare` so the CDP port is up before the sidecar picks an engine.
pub fn ensure_init(app: &tauri::AppHandle, parent: *mut c_void) {
    let mut st = match STATE.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if !st.load_attempted {
        st.load_attempted = true;
        match load_api(app) {
            Ok(api) => st.api = Some(api),
            Err(e) => {
                shell_log(app, &format!("cef-host skip: {e}"));
                write_status(app, 0, false, &e);
                return;
            }
        }
    }
    if st.inited {
        // log_host_ready() often inits with a NULL parent (CDP port first).
        // When the hole NSView shows up, hand it to C so the Alloy child
        // can be created after OnContextInitialized — do not re-enter
        // cef_initialize.
        if !parent.is_null() {
            if let Some(api) = st.api.as_ref() {
                let _ = unsafe { (api.attach)(parent, 800, 600) };
            }
        }
        return;
    }
    let (init, ready_fn, port_fn, last_error) = {
        let Some(api) = st.api.as_ref() else {
            return;
        };
        (api.init, api.ready, api.port, api.last_error)
    };
    let Some(fw) = frameworks_dir(app) else {
        return;
    };
    let Some(helper) = helper_exe(&fw) else {
        return;
    };
    let bundle = main_bundle().unwrap_or_else(|| fw.clone());
    let cache = cache_path(app);
    let _ = std::fs::create_dir_all(&cache);
    let port = free_port();
    let fw_c = match c_path(&fw) {
        Ok(s) => s,
        Err(e) => {
            write_status(app, 0, false, &e);
            return;
        }
    };
    let helper_c = match c_path(&helper) {
        Ok(s) => s,
        Err(e) => {
            write_status(app, 0, false, &e);
            return;
        }
    };
    let bundle_c = match c_path(&bundle) {
        Ok(s) => s,
        Err(e) => {
            write_status(app, 0, false, &e);
            return;
        }
    };
    let cache_c = match c_path(&cache) {
        Ok(s) => s,
        Err(e) => {
            write_status(app, 0, false, &e);
            return;
        }
    };
    let ok = unsafe {
        (init)(
            fw_c.as_ptr(),
            helper_c.as_ptr(),
            bundle_c.as_ptr(),
            cache_c.as_ptr(),
            port,
            parent,
        )
    };
    if ok == 0 {
        let err = unsafe {
            let p = last_error();
            if p.is_null() {
                String::new()
            } else {
                CStr::from_ptr(p).to_string_lossy().into_owned()
            }
        };
        shell_log(app, &format!("cef-host init failed: {err}"));
        write_status(app, port, false, &err);
        return;
    }
    st.inited = true;
    let ready = unsafe { (ready_fn)() } != 0;
    let live_port = unsafe { (port_fn)() };
    LIVE.store(ready, Ordering::SeqCst);
    write_status(app, live_port, ready, "");
    shell_log(
        app,
        &format!("cef-host ready port={live_port} parent={}", !parent.is_null()),
    );
}

pub fn install_hittest(webview: *mut c_void, wrapper: *mut c_void) {
    let st = match STATE.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if let Some(api) = st.api.as_ref() {
        unsafe { (api.install_hit)(webview, wrapper) };
    }
}

/// Resize / hide / hit-test the native child. No-op when the host never
/// came up (screencast tile stays opaque and on top).
pub fn apply(
    app: &tauri::AppHandle,
    parent: *mut c_void,
    webview: *mut c_void,
    visible: bool,
    width: i32,
    height: i32,
    passthrough: bool,
) {
    ensure_init(app, parent);
    install_hittest(webview, parent);
    let st = match STATE.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    let Some(api) = st.api.as_ref() else {
        return;
    };
    if !st.inited {
        return;
    }
    if visible {
        let _ = unsafe { (api.attach)(parent, width, height) };
        unsafe { (api.set_hidden)(0) };
        unsafe { (api.set_pass)(if passthrough { 1 } else { 0 }) };
    } else {
        unsafe { (api.set_hidden)(1) };
        unsafe { (api.set_pass)(0) };
    }
}

pub fn is_live() -> bool {
    LIVE.load(Ordering::SeqCst)
}
