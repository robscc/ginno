//! Shell-owned static UI: the splash / error data:-URL pages and the minimal
//! en/zh copy selection for isolated shell strings (no i18n catalog —
//! i18n-design.md §7).

use crate::common::ginno_home_path;

pub(crate) fn shell_lang<M: tauri::Manager<tauri::Wry>>(app: &M) -> &'static str {
    let text = std::fs::read_to_string(ginno_home_path(app).join("settings.json")).ok();
    let v = text
        .as_deref()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(t).ok());
    let lang = v
        .as_ref()
        .and_then(|v| v.get("language"))
        .and_then(|x| x.as_str());
    let legacy = v
        .as_ref()
        .and_then(|v| v.get("prompt_language"))
        .and_then(|x| x.as_str());
    let zh = match lang {
        Some("zh-CN") => true,
        Some(_) => false,
        None => legacy == Some("zh"),
    };
    if zh {
        "zh"
    } else {
        "en"
    }
}

/// 壳不持 catalog（i18n-design.md §7）：孤立文案用最小 en/zh 字面量二选一。
pub(crate) fn pick<'a>(lang: &str, en: &'a str, zh: &'a str) -> &'a str {
    if lang == "zh" {
        zh
    } else {
        en
    }
}

#[cfg(not(debug_assertions))]
const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Minimal base64 encoder (no extra dependency needed for one data: URL).
#[cfg(not(debug_assertions))]
pub(crate) fn base64(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64_ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(B64_ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64_ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64_ALPHABET[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

/// Self-contained loading page shown while the runtime boots.
///
/// Served from a data: URL so it needs no server; it polls the runtime's
/// /api/health and hands over to the app (same-origin with the API) once the
/// runtime is reachable. `__PORT__` is substituted at runtime.
#[cfg(not(debug_assertions))]
pub(crate) const SPLASH_HTML: &str = r#"<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { height: 100%; margin: 0; }
  body {
    background: #0b0d12; color: #e6e8ee;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .box { text-align: center; }
  .spin {
    width: 34px; height: 34px; margin: 0 auto 18px; border-radius: 50%;
    border: 3px solid rgba(255,255,255,.14); border-top-color: #7aa2ff;
    animation: r .9s linear infinite;
  }
  @keyframes r { to { transform: rotate(360deg); } }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 10px; letter-spacing: .5px; }
  p { font-size: 13px; color: #9aa0ad; margin: 0; min-height: 18px; }
</style>
</head>
<body>
<div class="box">
  <div class="spin"></div>
  <h1>Ginno</h1>
  <p id="s">__L_STARTING__</p>
</div>
<script>
  /* Static splash: no network calls (a data: page fetching loopback would be
     blocked by Private Network Access in some engines). The Tauri shell polls
     the runtime port and navigates this webview to the app once it is up; the
     script below only animates a status line while we wait. __L_*__ tokens
     are substituted by localize_shell_html (shell_lang picks en/zh). */
  var t0 = Date.now();
  function status(msg) { var el = document.getElementById('s'); if (el) el.textContent = msg; }
  function tick() {
    var s = Math.round((Date.now() - t0) / 1000);
    if (s > 120) {
      status('__L_SLOW_A__' + s + '__L_SLOW_B__');
    } else if (s > 8) {
      status('__L_WARMUP_A__' + s + '__L_WARMUP_B__');
    } else {
      status('__L_STARTING_A__' + s + '__L_STARTING_B__');
    }
    setTimeout(tick, 500);
  }
  tick();
</script>
</body>
</html>"#;

/// Error page shown when the runtime never came up within the 60s budget.
///
/// Same data:-URL constraints as SPLASH_HTML (no network JS, no Tauri IPC):
/// the 重试 button is a plain top-level navigation (PNA never gates those),
/// and a background poller in the shell auto-navigates to the app once the
/// port accepts — so recovery works even without clicking.
#[cfg(not(debug_assertions))]
pub(crate) const ERROR_HTML: &str = r#"<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { height: 100%; margin: 0; }
  body {
    background: #0a0a0f; color: #e9e9f0;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .box { text-align: center; width: min(420px, 90vw); }
  .ic {
    width: 48px; height: 48px; margin: 0 auto 16px; border-radius: 14px;
    background: rgba(239,68,68,.12); color: #ef4444;
    display: flex; align-items: center; justify-content: center;
  }
  h1 { font-size: 16px; font-weight: 600; margin: 0 0 8px; }
  p { font-size: 12.5px; color: #9a9aa6; margin: 0; }
  code {
    display: inline-block; margin-top: 10px; font: 11px/1.6 ui-monospace, monospace;
    color: #9a9aa6; background: #15151d; border: 1px solid #262632;
    border-radius: 6px; padding: 4px 8px;
  }
  .row { margin-top: 20px; }
  button {
    background: #8b5cf6; color: #fff; border: none; border-radius: 9px;
    padding: 8px 18px; font-size: 13px; cursor: pointer;
  }
  button:hover { filter: brightness(1.12); }
  .note { margin-top: 14px; font-size: 11px; color: #62626e; }
</style>
</head>
<body>
<div class="box">
  <div class="ic">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.3 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.7 3.86a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>
  </div>
  <h1>__L_ERR_TITLE__</h1>
  <p>__L_ERR_BODY__</p>
  <code>~/.ginno/logs/sidecar.log</code>
  <div class="row">
    <button onclick="location.href='http://127.0.0.1:__PORT__/'">__L_ERR_RETRY__</button>
  </div>
  <div class="note">__L_ERR_NOTE__</div>
</div>
</body>
</html>"#;

/// SPLASH/ERROR 页是 data: URL 静态页（runtime 未起，前端无法传参），文案由
/// shell_lang 的二选一结果替换 __L_*__ token（壳不持 catalog，i18n-design.md §7）。
#[cfg(not(debug_assertions))]
pub(crate) fn localize_shell_html(html: &str, lang: &str) -> String {
    let pairs: &[(&str, &str)] = &[
        ("__L_STARTING__", pick(lang, "Starting the runtime…", "正在启动运行时…")),
        ("__L_STARTING_A__", pick(lang, "Starting the runtime… (", "正在启动运行时…（")),
        ("__L_STARTING_B__", pick(lang, "s)", "s）")),
        ("__L_WARMUP_A__", pick(lang, "First launch takes a moment, loading dependencies… (", "首次启动需要加载依赖，请稍候…（")),
        ("__L_WARMUP_B__", pick(lang, "s)", "s）")),
        ("__L_SLOW_A__", pick(lang, "Startup is taking a while (", "启动耗时较久（")),
        ("__L_SLOW_B__", pick(lang, "s), still waiting… If it keeps failing, restart the app", "s），仍在等待…若持续失败请重启应用")),
        ("__L_ERR_TITLE__", pick(lang, "Runtime failed to start", "运行时启动失败")),
        ("__L_ERR_BODY__", pick(lang, "Timed out waiting for the sidecar (60s). Session data is unaffected.", "等待后端启动超时（60s）。会话数据不受影响。")),
        ("__L_ERR_RETRY__", pick(lang, "Retry", "重试")),
        ("__L_ERR_NOTE__", pick(lang, "The app will open automatically once the runtime is ready", "运行时就绪后应用会自动打开")),
    ];
    let mut out = html.to_string();
    for (token, text) in pairs {
        out = out.replace(token, text);
    }
    out
}
