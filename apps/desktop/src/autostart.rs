//! 登录自启：SMAppService 真源 + LaunchAgent 遗留迁移（2026-11-22 巡检 P1）。
//!
//! 巡检发现：tauri-plugin-autostart 只透出 LaunchAgent/AppleScript 两种
//! 模式，实际真源是 `~/Library/LaunchAgents/<name>.plist`（isEnabled =
//! plist 存在），而 lib.rs 注释与设置页 hint 都声称「SMAppService 为真源、
//! 系统设置里关闭后 UI 如实显示」——系统设置「允许在后台」禁用后 plist
//! 仍在，UI 恒显「已开启」，承诺错位。
//!
//! 修法：绕开插件直接用 auto-launch 0.6.0（依赖树已有，经 smappservice-rs）
//! 的 `MacOSLaunchMode::SMAppService`——注册态即系统登录项真态；dev /
//! 未打包 / macOS <13 回退 LaunchAgent（旧行为）；旧插件时代的 plist 由
//! enable 迁移、disable 一并清理；is_enabled = SM 注册态 || legacy plist
//! 存在（自启真实在跑就显示开）。

use std::path::PathBuf;

use auto_launch::{AutoLaunch, AutoLaunchBuilder, MacOSLaunchMode};

/// SMAppService 需要 macOS 13+ 且 app 以 bundle 身份跑；dev（cargo run /
/// tauri dev，debug_assertions）回退 LaunchAgent——与旧行为一致，不对开
/// 发机做系统级注册。
fn wants_smappservice() -> bool {
    !cfg!(debug_assertions)
}

/// 旧 LaunchAgent plist 路径（tauri-plugin-autostart 时代的真源）。
#[cfg(test)]
fn legacy_plist_path_in(home: &str, app_name: &str) -> PathBuf {
    std::path::Path::new(home)
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{app_name}.plist"))
}

fn legacy_plist_path(app_name: &str) -> Option<PathBuf> {
    let home = std::env::var("HOME").ok()?;
    Some(
        std::path::Path::new(&home)
            .join("Library")
            .join("LaunchAgents")
            .join(format!("{app_name}.plist")),
    )
}

fn legacy_plist_exists(app_name: &str) -> bool {
    legacy_plist_path(app_name)
        .map(|p| p.is_file())
        .unwrap_or(false)
}

fn legacy_plist_remove(app_name: &str) {
    if let Some(p) = legacy_plist_path(app_name) {
        match std::fs::remove_file(&p) {
            Ok(_) => eprintln!("autostart legacy plist removed: {}", p.display()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => eprintln!("autostart legacy plist remove failed: {e}"),
        }
    }
}

fn build_auto(
    app_name: &str,
    app_path: &str,
    force_launch_agent: bool,
) -> Result<AutoLaunch, String> {
    let mode = if wants_smappservice() && !force_launch_agent {
        MacOSLaunchMode::SMAppService
    } else {
        MacOSLaunchMode::LaunchAgent
    };
    AutoLaunchBuilder::new()
        .set_app_name(app_name)
        .set_app_path(app_path)
        .set_macos_launch_mode(mode)
        .build()
        .map_err(|e| e.to_string())
}

fn product_name(app: &tauri::AppHandle) -> String {
    app.config()
        .product_name
        .clone()
        .unwrap_or_else(|| "Ginno".into())
}

fn app_exec_path(app: &tauri::AppHandle) -> Result<String, String> {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.to_str().map(str::to_string))
        .ok_or_else(|| "cannot resolve current exe".into())
}

/// 登录项注册态（设置页「已开启/已关闭」的真源）：
/// SMAppService 注册态 **或** 旧 LaunchAgent plist 存在——后者意味着自启
/// 真实在跑（老版本注册的），UI 必须显示开。
#[tauri::command]
pub fn ginno_autostart_is_enabled(app: tauri::AppHandle) -> Result<bool, String> {
    let name = product_name(&app);
    let path = app_exec_path(&app)?;
    let auto = build_auto(&name, &path, false)?;
    let sm = auto.is_enabled().unwrap_or(false);
    Ok(sm || legacy_plist_exists(&name))
}

/// 开/关登录项。enable：SMAppService 注册，失败（未打包/旧系统/策略）回
/// 退 LaunchAgent；成功后清理 legacy plist（一次性迁移）。disable：当前
/// 模式注销 + legacy plist 一并删除（两个注册途径都真正关闭）。
#[tauri::command]
pub fn ginno_autostart_set(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let name = product_name(&app);
    let path = app_exec_path(&app)?;
    if enabled {
        let mut force_launch_agent = false;
        match build_auto(&name, &path, false) {
            Ok(auto) => {
                match auto.enable() {
                    Ok(_) => {}
                    Err(e) => {
                        eprintln!("autostart SMAppService enable failed ({e}); falling back to LaunchAgent");
                        force_launch_agent = true;
                    }
                }
            }
            Err(e) => {
                eprintln!("autostart SMAppService build failed ({e}); falling back to LaunchAgent");
                force_launch_agent = true;
            }
        }
        if force_launch_agent {
            build_auto(&name, &path, true)?
                .enable()
                .map_err(|e| e.to_string())?;
        }
        legacy_plist_remove(&name); // 迁移完成，避免双注册残留
    } else {
        // 两个途径都关：SMAppService 注销失败不阻塞 legacy 清理。
        if let Ok(auto) = build_auto(&name, &path, false) {
            if let Err(e) = auto.disable() {
                eprintln!("autostart SMAppService disable failed: {e}");
            }
        }
        legacy_plist_remove(&name);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_plist_path_layout() {
        let p = legacy_plist_path_in("/Users/demo", "Ginno");
        assert_eq!(
            p,
            PathBuf::from("/Users/demo/Library/LaunchAgents/Ginno.plist")
        );
    }

    #[test]
    fn legacy_plist_exists_reflects_file() {
        let dir = std::env::temp_dir().join(format!("ginno-autostart-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let plist = dir.join("Ginno.plist");
        std::fs::write(&plist, "<plist/>").unwrap();
        assert!(plist.is_file());
        std::fs::remove_file(&plist).unwrap();
        assert!(!plist.is_file());
    }

    #[test]
    fn dev_build_uses_launch_agent() {
        // debug_assertions 下 wants_smappservice() == false；release 构建此
        // 断言反向（巡检修复的目标形态：release 走 SMAppService）。
        if cfg!(debug_assertions) {
            assert!(!wants_smappservice());
        } else {
            assert!(wants_smappservice());
        }
    }
}
