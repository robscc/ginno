//! 退出确认门(quit gate):真退出(⌘Q / 菜单 Quit / app.exit)前问
//! sidecar「现在有什么在跑」(GET /api/running → running_work.py 聚合),
//! 有运行中 turn 或非 detached 后台命令时弹原生确认框拦一次。
//!
//! 调用方约定:本模块只在**非主线程**驱动(confirm_quit_or_proceed 由
//! lib.rs 的 ExitRequested 处理 spawn 的线程调用)——查 sidecar 是阻塞
//! HTTP;确认框用插件的非阻塞 show(内部自行 run_on_main_thread;
//! blocking_show 在主线程会冻死应用)。结论经 on_decided 回调交还:
//! confirmed=true → 调用方置 quitting 标志 + app.exit(0) 重入退出流程。

use std::time::Duration;

use serde::Deserialize;
use tauri::AppHandle;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::common::shell_log;
use crate::shell_ui::{pick, shell_lang};
use crate::sidecar::sidecar_port;

#[derive(Debug, Deserialize)]
struct RunningReport {
    #[serde(default)]
    sessions: Vec<RunningEntry>,
    #[serde(default)]
    tasks: Vec<RunningTask>,
}

#[derive(Debug, Deserialize)]
struct RunningEntry {
    #[serde(default)]
    title: String,
}

#[derive(Debug, Deserialize)]
struct RunningTask {
    #[serde(default)]
    id: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    command: String,
}

/// 查 sidecar 的运行中聚合。任何失败(未启动/已死/超时/解析不了)都按
/// 「没有运行中的工作」处理——确认门不能把退出卡死在一个不可达的
/// runtime 上(正常退出静默放行,不弹框)。
fn fetch_running() -> Option<RunningReport> {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(900))
        .build();
    let url = format!("http://127.0.0.1:{}/api/running", sidecar_port());
    let body = agent.get(&url).call().ok()?.into_string().ok()?;
    serde_json::from_str(&body).ok()
}

/// 确认框正文:计数句 + 至多 4 行明细(会话标题在前,命令缺标题时退化为
/// 命令首段 + 任务 id),溢出折成「+N more」。文案走 shell_ui 的 en/zh
/// 二选一(壳不持 catalog,i18n-design.md §7)。
fn confirm_body(r: &RunningReport, lang: &str) -> String {
    let ns = r.sessions.len();
    let nt = r.tasks.len();
    let zh = lang == "zh";
    let mut parts: Vec<String> = Vec::new();
    if ns > 0 {
        parts.push(
            pick(
                lang,
                &format!("{ns} session{}", if ns == 1 { "" } else { "s" }),
                &format!("{ns} 个会话"),
            )
            .to_string(),
        );
    }
    if nt > 0 {
        parts.push(
            pick(
                lang,
                &format!("{nt} background command{}", if nt == 1 { "" } else { "s" }),
                &format!("{nt} 个后台命令"),
            )
            .to_string(),
        );
    }
    // count 恒 >0 才会进对话框;0 分支纯防御。
    let mut body = if zh {
        format!("{}仍在运行。现在退出会中断它们。", parts.join("和"))
    } else {
        let be = if parts.len() == 1 { "is" } else { "are" };
        format!(
            "{} {be} still running. Quitting now will interrupt them.",
            parts.join(" and ")
        )
    };
    let mut lines: Vec<String> = r
        .sessions
        .iter()
        .filter_map(|s| {
            let t = s.title.trim();
            (!t.is_empty()).then(|| format!("• {t}"))
        })
        .collect();
    lines.extend(r.tasks.iter().map(|t| {
        let label = if t.title.trim().is_empty() {
            t.command
                .split_whitespace()
                .take(3)
                .collect::<Vec<_>>()
                .join(" ")
        } else {
            t.title.trim().to_string()
        };
        format!("• {label} ({})", t.id)
    }));
    if !lines.is_empty() {
        body.push_str("\n\n");
        body.push_str(&lines.iter().take(4).cloned().collect::<Vec<_>>().join("\n"));
        if lines.len() > 4 {
            body.push_str(&pick(
                lang,
                &format!("\n+{} more", lines.len() - 4),
                &format!("\n+{} 项", lines.len() - 4),
            ));
        }
    }
    body
}

/// 入口(后台线程):没有在跑的 → 直接放行;有 → 非阻塞原生确认框,
/// 用户点「Quit Anyway」才放行。`on_decided(app, confirmed)` 恰好被调用
/// 一次(取消也算决定),由调用方负责置标志/退出/解锁。
pub(crate) fn confirm_quit_or_proceed(
    app: AppHandle,
    on_decided: impl FnOnce(&AppHandle, bool) + Send + 'static,
) {
    let Some(report) = fetch_running() else {
        shell_log(
            &app,
            "quit gate: sidecar unreachable or idle — exiting silently",
        );
        on_decided(&app, true);
        return;
    };
    if report.sessions.is_empty() && report.tasks.is_empty() {
        shell_log(&app, "quit gate: nothing running — exiting silently");
        on_decided(&app, true);
        return;
    }
    shell_log(
        &app,
        &format!(
            "quit gate: {} session(s), {} task(s) running — confirming",
            report.sessions.len(),
            report.tasks.len()
        ),
    );
    let lang = shell_lang(&app);
    let body = confirm_body(&report, lang);
    let app_cb = app.clone();
    app.dialog()
        .message(body)
        .title(pick(lang, "Quit Ginno?", "退出 Ginno？"))
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            pick(lang, "Quit Anyway", "仍然退出").to_string(),
            pick(lang, "Cancel", "取消").to_string(),
        ))
        .show(move |confirmed| {
            if !confirmed {
                shell_log(&app_cb, "quit gate: user cancelled — staying alive");
            }
            on_decided(&app_cb, confirmed);
        });
}
