//! 托盘动画帧预检（2026-11-21）：在 include_bytes! 之前精确校验
//! frame_00..frame_11（逐名 + PNG magic，不是计数），缺失/错名/坏内容时
//! 报出明确坏帧清单，而不是让 12 处 include_bytes! 各自报一条二进制不匹配。
//!
//! 判据单一来源是 `scripts/check_tray_frames.sh`——Makefile `app` 目标的
//! 自愈预检与本处调用同一脚本，本地 make、直接 cargo、CI rust job
//! （ci.yml `cargo check (desktop shell)` 会跑 build.rs）三路共用一套判据。
//! bash 在所有构建环境（macOS dev 机 / macos-14 runner）必在；万一脚本
//! 或 bash 不可用，回落到下方内联 Rust 检查（仅逐名存在性，同一清单语义，
//! 注释与脚本互指，改判据两处都要同步）。

use std::path::Path;
use std::process::Command;

fn main() {
    precheck_tray_frames();
    tauri_build::build()
}

fn precheck_tray_frames() {
    // 帧被增/删/替换后必须重跑本 build.rs，否则「上次构建后才删帧」会被
    // cargo 指纹缓存跳过、直到 include_bytes! 才爆。目录 mtime 在文件增删
    // 时会变化，盯目录即可。
    println!("cargo:rerun-if-changed=icons/tray_anim");
    println!("cargo:rerun-if-changed=scripts/check_tray_frames.sh");

    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let dir = Path::new(&manifest_dir).join("icons/tray_anim");
    let script = Path::new(&manifest_dir).join("scripts/check_tray_frames.sh");

    if script.is_file() {
        if let Ok(output) = Command::new("bash")
            .arg(&script)
            .env("TRAY_ANIM_DIR", &dir)
            .output()
        {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            if !stdout.trim().is_empty() {
                println!("{stdout}");
            }
            if !stderr.trim().is_empty() {
                eprintln!("{stderr}");
            }
            if output.status.success() {
                return;
            }
            panic!(
                "tray_anim 帧预检失败（坏帧清单见上方 ❌ 行）。\n\
                 修复：`make app` 会自动精准重烘缺失帧；手动执行\n\
                 `python3 apps/desktop/scripts/gen_tray_rainbow.py --only <帧名...>`"
            );
        }
        // Command 失败（无 bash 等）→ 落到内联回退
    }

    inline_fallback_check(&dir);
}

/// 内联回退：与 check_tray_frames.sh 的「逐名存在性」判定保持同一清单语义
/// （不做 PNG magic / 多余文件检查——那是脚本的增强项）。
fn inline_fallback_check(dir: &Path) {
    let missing: Vec<String> = (0..12)
        .map(|i| format!("frame_{i:02}.png"))
        .filter(|name| !dir.join(name).is_file())
        .collect();
    if !missing.is_empty() {
        panic!(
            "tray_anim 缺帧（{}/12）：{} —— 需要 frame_00..frame_11，\
             修复用 apps/desktop/scripts/gen_tray_rainbow.py --only <帧名...>",
            missing.len(),
            missing.join(" ")
        );
    }
}
