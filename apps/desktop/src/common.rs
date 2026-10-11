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
