//! Dev hot-swap finisher — `docs/runtime-hot-swap-design.md` §3.4 swap+restart 段。
//!
//! 分工：`make dev-swap`（CLI）负责 build 段（make runtime → ditto 到
//! `dev-override/ginno-runtime.v{sha}-{ts}/` → symlink 原子切 → 写
//! `state.json = pending`）。本模块是**唯一有资格杀/拉 sidecar 的一方**：
//! 重启 sidecar（此时 spawn 梯子解析到新 symlink）→ 轮询 `/api/health`
//! 验证构建指纹 → 成功记 verified；失败把 symlink 切回旧目录再重启，
//! 双重失败删 symlink 落回 app 内置 bundle —— app 永远打得开。
//!
//! 触发链：runtime 广播 `runtime.dev_swap_pending` → 前端 invoke
//! `finish_dev_swap`（见 web 侧监听）；Debug 菜单也提供手动入口。
//!
//! **P2 合流注记（self-update-design.md）**：整 app 自更新的 relauncher
//! 可直接复用本模块的骨架——`state.json` 状态机（pending/verified/
//! rolled_back + 原子写）、`flip_link` 的 tmp+rename 原子切换、
//! `wait_fingerprint` 的指纹验证与「验证不过即回滚」语义。差异仅在：
//! relauncher 换的是 .app 整体（需要第三方进程接力），本模块换的是
//! runtime bundle（壳自己就是 supervisor）。

use serde::Serialize;
use serde_json::Value;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::common::{ginno_home_path, shell_log};
use crate::sidecar::{restart_sidecar, sidecar_port};
use tauri::{Emitter, Manager};

const RETAIN_VERSIONS: usize = 2;

#[derive(Debug, Serialize, Clone)]
pub struct DevSwapResult {
    pub ok: bool,
    /// verified fingerprint (on success) or the fingerprint we fell back to.
    pub fingerprint: Option<String>,
    pub rolled_back: bool,
    pub detail: String,
}

fn override_dir(app: &tauri::AppHandle) -> PathBuf {
    ginno_home_path(app).join("dev-override")
}

fn state_path(app: &tauri::AppHandle) -> PathBuf {
    override_dir(app).join("state.json")
}

fn link_path(app: &tauri::AppHandle) -> PathBuf {
    override_dir(app).join("ginno-runtime")
}

/// Atomically point the `ginno-runtime` symlink at `target_name` (a sibling
/// directory name inside dev-override/). tmp-symlink + rename(2) — readers
/// see either the old or the new target, never a missing link.
fn flip_link(app: &tauri::AppHandle, target_name: &str) -> std::io::Result<()> {
    let dir = override_dir(app);
    let link = dir.join("ginno-runtime");
    let tmp = dir.join(".ginno-runtime.tmp-link");
    let _ = std::fs::remove_file(&tmp);
    std::os::unix::fs::symlink(target_name, &tmp)?;
    std::fs::rename(&tmp, &link)
}

fn read_state(app: &tauri::AppHandle) -> Option<Value> {
    let raw = std::fs::read_to_string(state_path(app)).ok()?;
    serde_json::from_str(&raw).ok()
}

fn write_state(app: &tauri::AppHandle, mut state: Value) {
    state["updated"] = Value::from(chrono_like_ts());
    if let Ok(s) = serde_json::to_string_pretty(&state) {
        let p = state_path(app);
        let tmp = p.with_extension("json.tmp");
        if std::fs::write(&tmp, s).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

fn chrono_like_ts() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Minimal HTTP GET over a raw TcpStream (no http client dep). Returns the
/// parsed JSON body, or None on any transport/parse failure.
fn http_get_json(port: u16, path: &str, timeout: Duration) -> Option<Value> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let req = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = Vec::with_capacity(8 * 1024);
    stream.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    let body = text.split_once("\r\n\r\n").map(|(_, b)| b)?;
    serde_json::from_str(body).ok()
}

/// Poll /api/health until `build.fingerprint` matches `want` (or timeout).
/// Matching the fingerprint — not just `ok` — proves the NEW bundle is up.
///
/// `want_built`（来自 state.json 的 built_at，新脚本写入；旧 state 无此字段
/// 传 None 退化为仅比指纹）：同一棵脏工作树连续构建 fingerprint 相同，
/// spawn ladder 若落回 factory/旧 bundle，只比指纹会把错进程验成通过
/// （2026-11-04 shell.log 实锤：stale 提示后紧跟 verified）。加验 built_at
/// 才能证明起的就是这次构建的 bundle。
fn wait_fingerprint(
    port: u16,
    want: &str,
    want_built: Option<&str>,
    budget: Duration,
) -> Option<String> {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if let Some(v) = http_get_json(port, "/api/health", Duration::from_secs(5)) {
            let fp = v
                .pointer("/build/fingerprint")
                .and_then(Value::as_str)
                .map(str::to_string);
            let built = v
                .pointer("/build/built_at")
                .and_then(Value::as_str)
                .unwrap_or("");
            if let Some(fp) = fp {
                let built_ok = match want_built {
                    Some(w) if !w.is_empty() => built == w,
                    _ => true,
                };
                if fp == want && built_ok {
                    return Some(fp);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(750));
    }
    None
}

/// Delete oldest `ginno-runtime.v*` dirs (never the current symlink target),
/// keeping the newest RETAIN_VERSIONS.
fn prune_versions(app: &tauri::AppHandle) {
    let dir = override_dir(app);
    let current = std::fs::read_link(link_path(app))
        .map(|p| p.file_name().and_then(|n| n.to_str()).map(str::to_string))
        .unwrap_or(None);
    let mut versions: Vec<(std::time::SystemTime, String)> = std::fs::read_dir(&dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().to_string();
            if !name.starts_with("ginno-runtime.v") {
                return None;
            }
            if current.as_deref() == Some(name.as_str()) {
                return None;
            }
            let mtime = e
                .metadata()
                .and_then(|m| m.modified())
                .unwrap_or(std::time::UNIX_EPOCH);
            Some((mtime, name))
        })
        .collect();
    versions.sort();
    while versions.len() > RETAIN_VERSIONS.saturating_sub(1) {
        let (_, oldest) = versions.remove(0);
        let p = dir.join(&oldest);
        if let Err(e) = delete_dir_all(&p) {
            shell_log(app, &format!("dev-swap: prune {oldest}: {e}"));
        }
    }
}

fn delete_dir_all(p: &Path) -> std::io::Result<()> {
    std::fs::remove_dir_all(p)
}

/// The whole swap-finish flow. Runs on a background thread holding
/// RestartLock (same discipline as the debug-restart menu entry).
pub(crate) fn run_finish(app: &tauri::AppHandle) -> DevSwapResult {
    let port = sidecar_port();
    let Some(state) = read_state(app) else {
        return DevSwapResult {
            ok: false,
            fingerprint: None,
            rolled_back: false,
            detail: "no state.json (run `make dev-swap` first)".into(),
        };
    };
    if state.get("state").and_then(Value::as_str) != Some("pending") {
        return DevSwapResult {
            ok: false,
            fingerprint: None,
            rolled_back: false,
            detail: format!(
                "state is {:?}, not pending — nothing to finish",
                state.get("state").and_then(Value::as_str)
            ),
        };
    }
    let Some(sha) = state.get("sha").and_then(Value::as_str).map(str::to_string) else {
        return DevSwapResult {
            ok: false,
            fingerprint: None,
            rolled_back: false,
            detail: "pending state has no sha".into(),
        };
    };
    let old_target = state
        .get("old")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|s| !s.is_empty());

    shell_log(app, &format!("dev-swap: finishing swap to {sha}"));
    // The CLI already flipped the symlink before writing state=pending, so
    // restart_sidecar's spawn resolves through it onto the NEW bundle.
    restart_sidecar(app);

    // 新脚本把新 bundle 的 built_at 写进 state.json：与指纹一起严格比对，
    // 堵住「同一脏树连续构建指纹相同 → 验了 factory/旧 bundle 也算通过」
    // 的假阳性（2026-11-04 实锤）。旧 state 无此字段 → None → 仅比指纹。
    let want_built = state
        .get("built_at")
        .and_then(Value::as_str)
        .map(str::to_string);
    if let Some(fp) = wait_fingerprint(port, &sha, want_built.as_deref(), Duration::from_secs(90)) {
        let mut st = state;
        st["state"] = Value::from("verified");
        write_state(app, st);
        prune_versions(app);
        shell_log(app, &format!("dev-swap: verified {fp}"));
        return DevSwapResult {
            ok: true,
            fingerprint: Some(fp),
            rolled_back: false,
            detail: "verified".into(),
        };
    }

    // Failure → roll the symlink back to the previous version, restart again.
    shell_log(app, "dev-swap: fingerprint not verified — rolling back");
    let rolled = match &old_target {
        Some(old) if override_dir(app).join(old).is_dir() => {
            if flip_link(app, old).is_ok() {
                restart_sidecar(app);
                wait_fingerprint(
                    port,
                    old.split('.').next_back().unwrap_or(""),
                    None,
                    Duration::from_secs(60),
                )
                .is_some()
                    || http_get_json(port, "/api/health", Duration::from_secs(5))
                        .map(|v| v.get("ok").and_then(Value::as_bool) == Some(true))
                        .unwrap_or(false)
            } else {
                false
            }
        }
        _ => false,
    };
    if !rolled {
        // Double failure: drop the override entirely — the spawn ladder falls
        // back to the app-bundled runtime. The app must always open.
        shell_log(
            app,
            "dev-swap: rollback failed — dropping override, factory bundle next",
        );
        let _ = std::fs::remove_file(link_path(app));
        restart_sidecar(app);
    }
    let mut st = read_state(app).unwrap_or(state);
    st["state"] = Value::from(if rolled {
        "rolled_back"
    } else {
        "rolled_back_factory"
    });
    write_state(app, st);
    DevSwapResult {
        ok: false,
        fingerprint: None,
        rolled_back: true,
        detail: if rolled {
            "new bundle failed health check; previous version restored".into()
        } else {
            "rollback also failed; override dropped, factory bundle active".into()
        },
    }
}

// ---- G1 unattended watcher（channel-devswap-unattended-design.md §1.3 L3/L4）----

/// 判定 state.json 是否满足无头 finish 条件：
/// state==pending && unattended=="go" && age < 600s。
fn unattended_go(state: &Value) -> bool {
    if state.get("state").and_then(Value::as_str) != Some("pending") {
        return false;
    }
    if state.get("unattended").and_then(Value::as_str) != Some("go") {
        return false;
    }
    let created = state
        .get("created")
        .and_then(Value::as_i64)
        .unwrap_or(i64::MAX);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    created > 0 && now.saturating_sub(created) < 600
}

/// 单次判定（L3 轮询 / L4 冷启动共用）：满足条件即 try_lock → run_finish。
/// unattended 字段缺省/非 "go" 一律不动——手动 dev-swap 的桌面确认弹窗
/// 路径零变化（回归红线）。
fn try_unattended_finish(app: &tauri::AppHandle) {
    let Some(state) = read_state(app) else { return };
    if !unattended_go(&state) {
        return;
    }
    let Some(lock) = app.try_state::<crate::sidecar::RestartLock>() else {
        return;
    };
    let Ok(_guard) = lock.0.try_lock() else {
        return; // 前端 invoke 或另一次重启在途——让位
    };
    let result = run_finish(app);
    let _ = app.emit("dev-swap:result", result);
}

/// 壳启动后 spawn 一次常驻 watcher：每 5s 轮询 state.json（L3 兜 webview
/// 崩溃/挂起；L2 前端直通更快）。进程内只 spawn 一次。
pub fn spawn_unattended_watcher(app: tauri::AppHandle) {
    std::thread::Builder::new()
        .name("devswap-unattended".into())
        .spawn(move || {
            // L4 冷启动判定：壳重启窗口内用户回了 y（sidecar 已写 state），
            // 这里立即补执行（age 守卫防误吞陈旧 pending）。
            try_unattended_finish(&app);
            loop {
                std::thread::sleep(std::time::Duration::from_secs(5));
                try_unattended_finish(&app);
            }
        })
        .ok();
}

#[tauri::command]
pub async fn finish_dev_swap(app: tauri::AppHandle) -> Result<DevSwapResult, String> {
    let handle = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        // Same lock discipline as the debug-restart menu entry: one swap at a
        // time, never racing an in-flight restart.
        let Some(lock) = handle.try_state::<crate::sidecar::RestartLock>() else {
            return DevSwapResult {
                ok: false,
                fingerprint: None,
                rolled_back: false,
                detail: "RestartLock not managed".into(),
            };
        };
        let Ok(_guard) = lock.0.try_lock() else {
            return DevSwapResult {
                ok: false,
                fingerprint: None,
                rolled_back: false,
                detail: "another restart/swap is already in flight".into(),
            };
        };
        let result = run_finish(&handle);
        let _ = handle.emit("dev-swap:result", result.clone());
        result
    })
    .await
    .map_err(|e| format!("join: {e}"))
}
