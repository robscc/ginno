//! Dock badge (agent-team-design.md §5.3.1): ONLY blocked / done bubble up —
//! working stays on the team row inside the app, never the Dock. Polls
//! `GET /api/teams/badge` on the sidecar every 5s over plain TCP (loopback,
//! no TLS → no HTTP dependency) and mirrors the `needs` count onto the macOS
//! Dock badge. Sidecar down → badge cleared (nothing needs you).

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use tauri::Manager;

use crate::sidecar::{sidecar_listening, SIDECAR_PORT};

const POLL_S: u64 = 5;

pub(crate) fn spawn_badge_watcher(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut last: i64 = -1; // -1 = unknown → force first apply
        loop {
            let needs = if sidecar_listening() {
                fetch_needs().unwrap_or(0)
            } else {
                0
            };
            if needs != last {
                last = needs;
                if let Err(e) = apply_badge(&app, needs) {
                    eprintln!("badge apply failed: {e}");
                }
            }
            std::thread::sleep(Duration::from_secs(POLL_S));
        }
    });
}

/// Minimal HTTP/1.0 GET + robust `"needs":N` scrape (no JSON crate on this
/// side; the payload is a tiny fixed-shape object).
fn fetch_needs() -> Result<i64, ()> {
    let mut stream = TcpStream::connect(("127.0.0.1", SIDECAR_PORT)).map_err(|_| ())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|_| ())?;
    let req = format!("GET /api/teams/badge HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).map_err(|_| ())?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).map_err(|_| ())?;
    let body = String::from_utf8_lossy(&buf);
    let key = body.find("\"needs\"").ok_or(())?;
    let rest = &body[key + 7..];
    let colon = rest.find(':').ok_or(())?;
    let after = rest[colon + 1..].trim_start();
    let end = after
        .find(|c: char| !c.is_ascii_digit() && c != '-')
        .unwrap_or(after.len());
    after[..end].parse::<i64>().map_err(|_| ())
}

fn apply_badge(app: &tauri::AppHandle, needs: i64) -> tauri::Result<()> {
    #[cfg(target_os = "macos")]
    {
        if let Some(w) = app.get_webview_window("main") {
            // 0/None clears the badge; negative never happens (defensive).
            return w.set_badge_count(if needs > 0 { Some(needs) } else { None });
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app, needs); // taskbar badges unsupported here (Windows path)
    }
    Ok(())
}
