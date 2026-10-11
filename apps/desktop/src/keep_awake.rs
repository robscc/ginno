// ---------------------------------------------------------------------------
// Scheduled-tasks keep-awake — docs/scheduled-tasks-design.md §3.8.
//
// `caffeinate -i` 只挡闲置系统睡眠（不是屏幕变暗/锁定，也不是合盖）。开关的
// 真值是 runtime 侧 schedules.json 的 keep_awake 字段；web 在加载 /scheduled
// 数据后同步一次，壳端这两个 command 只是进程的持有者。子进程随壳退出自动
// 回收（RunEvent::Exit 里也会显式 kill），无需额外退出钩子。
// ---------------------------------------------------------------------------

use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::Manager;

/// 定时任务 keep-awake（docs/scheduled-tasks-design.md §3.8）：常驻
/// `caffeinate -i` 子进程，只挡闲置系统睡眠（屏幕可暗可锁）。真值在 runtime 的
/// schedules.json —— web 启动加载后 invoke `set_keep_awake` 同步一次；壳只负责
/// 持有/回收这个子进程（随壳退出自动释放，Exit 钩子里再显式 kill 一下）。
pub(crate) struct KeepAwake(pub(crate) Mutex<Option<Child>>);

/// 开 = 未持有活着的 caffeinate 就 spawn 并持有；关 = kill 并清理。幂等：
/// 重复开不动已有的子进程（但里面死掉的先回收再重开）。
#[tauri::command]
pub(crate) fn set_keep_awake(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let Some(state) = app.try_state::<KeepAwake>() else {
        return Err("keep-awake state not initialized".to_string());
    };
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "keep-awake state lock unavailable".to_string())?;
    let alive = guard
        .as_mut()
        .map(|c| matches!(c.try_wait(), Ok(None)))
        .unwrap_or(false);
    if enabled {
        if alive {
            return Ok(());
        }
        // 回收已死（或僵尸）的旧子进程再开新的。
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        let child = Command::new("/usr/bin/caffeinate")
            .arg("-i")
            .stdin(Stdio::null())
            .spawn()
            .map_err(|e| format!("Failed to start caffeinate: {e}"))?;
        *guard = Some(child);
    } else if let Some(mut child) = guard.take() {
        // 只在真的持有过时才 kill；已死/不存在都当成功（幂等关）。
        let _ = child.kill();
        let _ = child.wait();
    }
    Ok(())
}

/// 子进程是否存活（UI 回显 / 诊断用）。状态未托管或子进程已退出都算关。
#[tauri::command]
pub(crate) fn get_keep_awake(app: tauri::AppHandle) -> bool {
    app.try_state::<KeepAwake>()
        .and_then(|state| {
            state
                .0
                .lock()
                .ok()
                .map(|mut guard| {
                    guard
                        .as_mut()
                        .map(|c| matches!(c.try_wait(), Ok(None)))
                        .unwrap_or(false)
                })
        })
        .unwrap_or(false)
}
