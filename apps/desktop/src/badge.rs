//! Dock badge: needs = unread sessions + unseen schedule runs + team
//! blocked/done (agent-team-design.md §5.3.1 — working stays on the team row
//! inside the app, never the Dock). Polls `GET /api/badge` on the sidecar
//! every 5s over plain TCP (loopback, no TLS → no HTTP dependency) and
//! mirrors the `needs` count onto the macOS Dock badge. Sidecar down → badge
//! cleared (nothing needs you).
//!
//! The same poll also feeds the menu-bar tray a `running` indicator
//! (`▶N` status-item title while ≥1 turn is streaming, cleared at 0) —
//! the one place "working" is allowed to surface outside the app window.
//! Layering: Dock badge = needs (wants you), tray title = running (is busy).
//!
//! And the pinned-sessions quick-nav: the poll carries `pinned_sessions`
//! (id+title, ≤8) and rebuilds the tray menu whenever that list changes
//! (tray::rebuild_menu). Body parsing goes through serde_json (already a
//! desktop dependency) — the old `"key":N` string scrape was fine for two
//! ints, not for an array.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use tauri::Manager;

use crate::sidecar::{sidecar_listening, sidecar_port};

const POLL_S: u64 = 5;

/// One sidebar-pinned session, as carried by /api/badge.
#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
pub(crate) struct PinnedSession {
    pub id: String,
    #[serde(default)]
    pub title: String,
}

/// What the poll extracts from /api/badge.
#[derive(Debug, Clone, PartialEq, Default)]
pub(crate) struct BadgeInfo {
    pub needs: i64,
    pub running: i64,
    pub pinned: Vec<PinnedSession>,
}

/// Strict subset the parser demands: `needs`/`running` missing → Err (stale
/// sidecar, fail-to-zero convention); `pinned_sessions` missing → empty.
#[derive(serde::Deserialize)]
struct BadgePayload {
    needs: i64,
    running: i64,
    #[serde(default)]
    pinned_sessions: Vec<PinnedSession>,
}

pub(crate) fn spawn_badge_watcher(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        // -1s = unknown → force the first apply; empty pinned = initial menu
        // (built by install_tray) until the first successful poll lands.
        let mut last = BadgeInfo {
            needs: -1,
            running: -1,
            pinned: Vec::new(),
        };
        loop {
            // Sidecar down → counters clear + pinned section empties (the
            // existing "nothing needs you" convention; menu falls back to
            // its no-pins placeholder row).
            let info = if sidecar_listening() {
                fetch_badge().unwrap_or_default()
            } else {
                BadgeInfo::default()
            };
            if info.needs != last.needs {
                if let Err(e) = apply_badge(&app, info.needs) {
                    eprintln!("badge apply failed: {e}");
                }
            }
            if info.running != last.running {
                if let Err(e) = apply_tray_running(&app, info.running) {
                    eprintln!("tray running apply failed: {e}");
                }
            }
            if info.pinned != last.pinned {
                if let Err(e) = crate::tray::rebuild_menu(&app, &info.pinned) {
                    eprintln!("tray menu rebuild failed: {e}");
                }
            }
            last = info;
            std::thread::sleep(Duration::from_secs(POLL_S));
        }
    });
}

/// Minimal HTTP/1.0 GET over plain TCP (loopback, no TLS → no HTTP client
/// dependency). Response body parsed as JSON (see module docs).
fn fetch_badge() -> Result<BadgeInfo, ()> {
    let mut stream = TcpStream::connect(("127.0.0.1", sidecar_port())).map_err(|_| ())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|_| ())?;
    let req = format!("GET /api/badge HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).map_err(|_| ())?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).map_err(|_| ())?;
    parse_badge(&String::from_utf8_lossy(&buf))
}

/// Parse the response (kept separate from the IO for tests). Accepts a full
/// HTTP response (headers stripped at the first blank line) or a bare JSON
/// body. Strict on needs/running (stale sidecar → Err), lenient on
/// pinned_sessions (missing → empty).
fn parse_badge(body: &str) -> Result<BadgeInfo, ()> {
    let json = match body.find("\r\n\r\n") {
        Some(at) => &body[at + 4..],
        None => body,
    };
    let p: BadgePayload = serde_json::from_str(json.trim_start()).map_err(|_| ())?;
    Ok(BadgeInfo {
        needs: p.needs,
        running: p.running,
        pinned: p.pinned_sessions,
    })
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

/// Menu-bar running indicator: NSStatusItem title `▶N` while ≥1 turn is
/// streaming, `None` (cleared) at 0 — no icon assets needed, macOS renders
/// the title next to the tray icon and adapts to light/dark mode. No tray
/// (install failed / not yet built) → silent no-op. `set_title` is
/// unsupported on Windows tray and needs an icon on Linux; this app ships
/// macOS-only, so a plain call keeps it honest there.
fn apply_tray_running(app: &tauri::AppHandle, running: i64) -> tauri::Result<()> {
    #[cfg(target_os = "macos")]
    {
        if let Some(tray) = app.tray_by_id("ginno-tray") {
            if running > 0 {
                tray.set_title(Some(format!("▶{running}")))?;
                // 彩虹描边流光（2026-11-21 方案 2）：有 turn 在跑即启动。
                // start 幂等（世代计数），stop 由 running 归零分支触发。
                crate::tray_anim::start(app);
            } else {
                tray.set_title(None::<&str>)?;
                crate::tray_anim::stop(app);
            }
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app, running); // Windows tray has no title; Linux-only app path
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const BODY: &str = concat!(
        "HTTP/1.0 200 OK\r\ncontent-type: application/json\r\n\r\n",
        "{\"ok\":true,\"needs\":3,\"sessions\":3,\"schedule_runs\":0,",
        "\"teams\":0,\"blocked\":0,\"done_unread\":0,\"running\":2,",
        "\"pinned_sessions\":[{\"id\":\"s1\",\"title\":\"周报\"},{\"id\":\"s2\",\"title\":\"\"}]}",
    );

    #[test]
    fn parses_needs_running_and_pinned() {
        let info = parse_badge(BODY).unwrap();
        assert_eq!(info.needs, 3);
        assert_eq!(info.running, 2);
        assert_eq!(
            info.pinned,
            vec![
                PinnedSession {
                    id: "s1".into(),
                    title: "周报".into()
                },
                PinnedSession {
                    id: "s2".into(),
                    title: String::new()
                },
            ]
        );
    }

    #[test]
    fn pinned_missing_defaults_empty() {
        // New field on an older-but-running sidecar: no crash, empty section.
        let body = concat!("HTTP/1.0 200 OK\r\n\r\n", "{\"needs\":3,\"running\":0}");
        let info = parse_badge(body).unwrap();
        assert_eq!(info.pinned, Vec::new());
    }

    #[test]
    fn old_sidecar_without_running_fails_to_zero() {
        // Pre-`running` payload → parse error → caller's unwrap_or_default:
        // stale sidecar shows no indicator, same convention as needs.
        let body = concat!(
            "HTTP/1.0 200 OK\r\n\r\n",
            "{\"ok\":true,\"needs\":1,\"sessions\":1,\"schedule_runs\":0,",
            "\"teams\":0,\"blocked\":0,\"done_unread\":0}"
        );
        assert!(parse_badge(body).is_err());
        assert_eq!(parse_badge(body).unwrap_or_default(), BadgeInfo::default());
    }

    #[test]
    fn bare_json_body_without_headers_parses() {
        let body = "{\"needs\":0,\"running\":1,\"pinned_sessions\":[]}";
        let info = parse_badge(body).unwrap();
        assert_eq!((info.needs, info.running), (0, 1));
        assert!(info.pinned.is_empty());
    }
}
