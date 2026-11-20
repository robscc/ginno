//! 托盘彩虹描边动画（方案 2「描边流光」，2026-11-21 用户选定）。
//!
//! 视觉契约：duck 保持原色，轮廓上一道 conic 彩虹光带随帧推进绕边流动
//! ——有 turn 在跑时启动，空闲即停并恢复原始 icon。帧资产由
//! `apps/desktop/scripts/gen_tray_rainbow.py` 预烘（12 帧 64px，编译期内
//! 嵌进二进制，不依赖运行时文件）；12 帧 × 170ms ≈ 2.04s/圈。
//!
//! 生命周期由 `badge.rs` 的轮询驱动：`running` 0→>0 时 [`start`]，
//! >0→0（或 sidecar 失联）时 [`stop`]。并发模型：原子世代计数器——
//! 每次 start/stop 都推进世代，动画线程发现世代失配即自杀，天然收敛
//! 为「至多一个线程在循环、stop 必然恢复静态 icon」。

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use tauri::AppHandle;

/// 12 帧动画（phase 0°→330°，步进 30°）。
const FRAMES: [&[u8]; 12] = [
    include_bytes!("../icons/tray_anim/frame_00.png"),
    include_bytes!("../icons/tray_anim/frame_01.png"),
    include_bytes!("../icons/tray_anim/frame_02.png"),
    include_bytes!("../icons/tray_anim/frame_03.png"),
    include_bytes!("../icons/tray_anim/frame_04.png"),
    include_bytes!("../icons/tray_anim/frame_05.png"),
    include_bytes!("../icons/tray_anim/frame_06.png"),
    include_bytes!("../icons/tray_anim/frame_07.png"),
    include_bytes!("../icons/tray_anim/frame_08.png"),
    include_bytes!("../icons/tray_anim/frame_09.png"),
    include_bytes!("../icons/tray_anim/frame_10.png"),
    include_bytes!("../icons/tray_anim/frame_11.png"),
];

const FRAME_MS: u64 = 170;
const TRAY_ID: &str = "ginno-tray";

/// 世代计数：start 时 +1 并被新线程持有；stop 时再 +1 使所有旧线程
/// 失效。static 而非 App state——线程自旋生命周期跨 tray 重建，与
/// Tauri 状态托管无交集。
static GEN: AtomicU64 = AtomicU64::new(0);

/// 启动描边流光（幂等：已在跑则只推进相位循环，不叠加线程）。
pub fn start(app: &AppHandle) {
    let gen = GEN.fetch_add(1, Ordering::SeqCst) + 1;
    let app = app.clone();
    std::thread::spawn(move || loop {
        if GEN.load(Ordering::SeqCst) != gen {
            return; // 被 stop 或更新的 start 替换
        }
        let Some(tray) = app.tray_by_id(TRAY_ID) else {
            return;
        };
        for bytes in FRAMES {
            if GEN.load(Ordering::SeqCst) != gen {
                restore(&app);
                return;
            }
            match tauri::image::Image::from_bytes(bytes) {
                Ok(img) => {
                    let _ = tray.set_icon(Some(img));
                }
                Err(e) => {
                    eprintln!("tray_anim frame decode failed: {e}");
                    restore(&app);
                    return;
                }
            }
            std::thread::sleep(Duration::from_millis(FRAME_MS));
        }
    });
}

/// 停止动画并恢复原始 icon（第 0 相位 ≠ 原图——conic 相位 0° 时轮廓
/// 右侧是红色，不是「无描边」，静态态必须用 default_window_icon）。
pub fn stop(app: &AppHandle) {
    GEN.fetch_add(1, Ordering::SeqCst);
    restore(app);
}

fn restore(app: &AppHandle) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        if let Some(icon) = app.default_window_icon() {
            let _ = tray.set_icon(Some(icon.clone()));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twelve_frames_embedded() {
        assert_eq!(FRAMES.len(), 12);
        // PNG 魔数头，防 include_bytes 路径指空/指错
        for (i, b) in FRAMES.iter().enumerate() {
            assert_eq!(&b[..4], b"\x89PNG", "frame {i} not a PNG");
        }
    }
}
