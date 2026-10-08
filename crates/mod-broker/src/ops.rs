//! The op table (design §3.3): broker-local implementations for everything
//! security-sensitive or cross-mod shared (state/store/clock/fs/http/process/
//! env/ui), a forward to the Python runtime for everything that needs runtime
//! facts (session/prompt/tool/command/mcp/model/agent/turn), and explicit
//! `no-implementation` answers for the ops this host does not serve. Grants
//! are checked before the chain runs, so a denied capability never reaches
//! any mod, let alone the implementation.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value};
use std::sync::Arc;
use tokio::io::AsyncReadExt;
use tokio::sync::oneshot;

use crate::chain::{run_from, ChainCore, ChainCtx};
use crate::protocol::{
    RpcError, CODE_DENIED, CODE_ERROR, FS_MAX_BYTES, HTTP_MAX_BYTES, PROCESS_OUTPUT_MAX_BYTES,
};
use crate::Broker;

/// `$`-in-`$` nesting cap. DSH has none (same-process recursion bottoms out);
/// here each level is real frames and processes, so a bound stops pathological
/// middleware recursion.
pub const MAX_OP_DEPTH: usize = 16;

/// The namespaces forwarded to the Python runtime (design §3.3 table).
pub const FORWARD_NAMESPACES: &[&str] =
    &["session", "prompt", "tool", "command", "mcp", "model", "agent", "turn"];

/// Ops registered as known events that this host deliberately does not serve;
/// every unlisted op lands here too (design §3.3, non-goals §1).
const UNSERVED_OPS: &[&str] = &[
    "audio.play", "audio.speak",
    "telemetry.log", "telemetry.mark",
    "settings.read",
    "env.set",
    "fs.ancestors",
    "ui.blit", "ui.focus", "ui.scroll", "ui.message",
    "config.list", "config.set",
    "model.fork", // P2
];

/// One `$` call from a runner: grant gate, middleware chain through the mods
/// loaded before the caller, then the op — with the `{value}`/`{deny}`
/// unwrapping DSH's `engine.invoke` does.
pub async fn invoke_op(
    broker: Arc<Broker>,
    caller: &str,
    op: &str,
    session: Option<String>,
    args: Value,
    invocation: Option<String>,
    depth: usize,
) -> Result<Value, RpcError> {
    if let Some(reason) = broker.grants_for(caller).check(op, &args) {
        // Point at the fix: grants live in settings, on the Mods page (§8).
        return Err(RpcError::new(
            CODE_DENIED,
            format!("{reason} (grants for a mod are configured on the Mods settings page)"),
        ));
    }
    if depth > MAX_OP_DEPTH {
        return Err(RpcError::new(CODE_ERROR, format!("{op}: mods call depth exceeded")));
    }
    let raised_by = broker.registry.read().unwrap().order_of(caller);
    // `tool.call` reaches the earlier mods once the host raises it from the
    // tool pipeline, not here as well (design §15.2 / engine.invoke).
    let hooks = if op == "tool.call" {
        Vec::new()
    } else {
        broker.registry.read().unwrap().select(op, raised_by)
    };
    let hook_budget_ms = broker.config.read().unwrap().budget_for(op);
    let catch_budget_ms = broker.config.read().unwrap().catch_ms;
    let ctx = Arc::new(ChainCtx {
        invocation: invocation.unwrap_or_else(|| broker.next_invocation()),
        broker,
        event: op.to_string(),
        session,
        raised_by,
        core: ChainCore::Op { op: op.to_string(), caller: caller.to_string() },
        validate_tool_call: false,
        original: Value::Null,
        shared: None,
        hook_budget_ms,
        catch_budget_ms,
        depth,
        max_depth: MAX_OP_DEPTH,
    });
    let result = run_from(&ctx, &hooks, 0, args).await?;
    unwrap_op_answer(op, &result)
}

/// DSH `engine.invoke`'s answer rules: the chain must settle with an object
/// carrying `value` or `deny`; a string `deny` refuses the call.
fn unwrap_op_answer(op: &str, result: &Value) -> Result<Value, RpcError> {
    let neither = || {
        RpcError::new(CODE_ERROR, format!("{op}: a hook returned neither {{ value }} nor {{ deny }}"))
    };
    match result {
        Value::Object(fields) => {
            if let Some(deny) = fields.get("deny") {
                if let Some(reason) = deny.as_str() {
                    return Err(RpcError::new(CODE_DENIED, format!("{op} refused: {reason}")));
                }
                // A non-string deny is not a refusal: DSH returns answer.value
                // (undefined when absent), which surfaces as null.
                return Ok(fields.get("value").cloned().unwrap_or(Value::Null));
            }
            match fields.get("value") {
                Some(value) => Ok(value.clone()),
                None => Err(neither()),
            }
        }
        _ => Err(neither()),
    }
}

fn no_implementation(op: &str) -> RpcError {
    RpcError::no_implementation(op)
}

fn need_string<'a>(input: &'a Value, field: &str, what: &str) -> Result<&'a str, RpcError> {
    input.get(field).and_then(Value::as_str).ok_or_else(|| {
        RpcError::new(CODE_ERROR, format!("{what} must be a string"))
    })
}

/// The engine behavior for one mods API call (the bottom of the chain).
pub async fn op_core(ctx: &Arc<ChainCtx>, op: &str, caller: &str, input: Value) -> Result<Value, RpcError> {
    let broker = &ctx.broker;
    match op {
        // ---- per-session state (design §15.4) ----
        "state.get" => {
            let slot = state_slot(&input)?;
            let key = ctx.session.clone().unwrap_or_default();
            broker.surfaces_state_read(&key, &slot, caller);
            // DSH's opCore shape: the mod destructures `{ value }` from the
            // answer. Cross-process, a missing (or null-stored) slot arrives
            // as `{value: null}` — the runner's `$` shim turns that into
            // `{value: undefined}`, restoring DSH's destructuring defaults.
            Ok(json!({ "value": broker.state_get(&key, &slot) }))
        }
        "state.set" => {
            let slot = state_slot(&input)?;
            let key = ctx.session.clone().unwrap_or_default();
            let value = input.get("value").cloned().unwrap_or(Value::Null);
            broker.state_set(&key, &slot, value);
            broker.surfaces_state_written(&key, &slot);
            Ok(Value::Null)
        }

        // ---- durable store ----
        "store.get" => {
            let key = need_string(&input, "key", "$.store.get key")?;
            Ok(broker.store.get(caller, key))
        }
        "store.set" => {
            let key = need_string(&input, "key", "$.store.set key")?;
            let value = input.get("value").cloned().unwrap_or(Value::Null);
            broker.store.set(caller, key, value).map_err(RpcError::new_error)?;
            Ok(Value::Null)
        }
        "store.delete" => {
            let key = need_string(&input, "key", "$.store.delete key")?;
            broker.store.delete(caller, key).map_err(RpcError::new_error)?;
            Ok(Value::Null)
        }
        "store.keys" => Ok(json!(broker.store.keys(caller))),

        // ---- clock ----
        "clock.now" => Ok(json!(now_ms())),
        "clock.sleep" => {
            let ms = input.get("ms").and_then(Value::as_u64).ok_or_else(|| {
                RpcError::new(CODE_ERROR, "$.clock.sleep needs a non-negative number of milliseconds".to_string())
            })?;
            tokio::time::sleep(std::time::Duration::from_millis(ms.min(60_000))).await;
            Ok(Value::Null)
        }
        "clock.after" => {
            let ms = ms_field(&input, "$.clock.after")?;
            let timer = broker.timers_of(&ctx.session, caller).after(ms).await;
            Ok(json!({ "timer": timer }))
        }
        "clock.every" => {
            let ms = ms_field(&input, "$.clock.every")?;
            let timer = broker.timers_of(&ctx.session, caller).every(ms).await;
            Ok(json!({ "timer": timer }))
        }
        "clock.cancel" => {
            let timer = need_string(&input, "timer", "$.clock.cancel timer")?;
            broker.timers_of(&ctx.session, caller).cancel(timer);
            Ok(Value::Null)
        }

        // ---- files, root-limited to the workspace ----
        "fs.read" => {
            let path = need_string(&input, "path", "$.fs.read path")?;
            let target = broker.workspace_path(path)?;
            let metadata = std::fs::metadata(&target)
                .map_err(|e| RpcError::new(CODE_ERROR, format!("$.fs.read: {path}: {e}")))?;
            if metadata.len() > FS_MAX_BYTES as u64 {
                return Err(RpcError::new(CODE_ERROR, format!("$.fs.read: {path} is larger than {FS_MAX_BYTES} bytes")));
            }
            let bytes = std::fs::read(&target)
                .map_err(|e| RpcError::new(CODE_ERROR, format!("$.fs.read: {path}: {e}")))?;
            Ok(Value::String(String::from_utf8_lossy(&bytes).into_owned()))
        }
        "fs.write" => {
            let path = need_string(&input, "path", "$.fs.write path")?;
            let text = need_string(&input, "text", "$.fs.write text")?;
            if text.len() > FS_MAX_BYTES {
                return Err(RpcError::new(CODE_ERROR, format!("$.fs.write: content is larger than {FS_MAX_BYTES} bytes")));
            }
            let target = broker.workspace_path(path)?;
            std::fs::write(&target, text)
                .map_err(|e| RpcError::new(CODE_ERROR, format!("$.fs.write: {path}: {e}")))?;
            Ok(Value::Null)
        }
        "fs.list" => {
            let path = input.get("path").and_then(Value::as_str).unwrap_or(".");
            let target = broker.workspace_path(path)?;
            let entries = std::fs::read_dir(&target)
                .map_err(|e| RpcError::new(CODE_ERROR, format!("$.fs.list: {path}: {e}")))?;
            let mut out = Vec::new();
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                let is_link = entry.file_type().map(|t| t.is_symlink()).unwrap_or(false);
                let followed = entry.metadata().ok();
                let kind = match &followed {
                    Some(meta) if meta.is_dir() => "dir",
                    Some(meta) if meta.is_file() => "file",
                    _ => "other",
                };
                out.push(json!({
                    "name": name,
                    "kind": kind,
                    "size": followed.as_ref().map(|meta| meta.len()).unwrap_or(0),
                    "isLink": is_link,
                }));
            }
            Ok(Value::Array(out))
        }
        "fs.exists" => {
            let path = need_string(&input, "path", "$.fs.exists path")?;
            match broker.workspace_path(path) {
                Ok(target) => Ok(json!(target.exists())),
                Err(_) => Ok(json!(false)),
            }
        }
        "fs.stat" => {
            let path = need_string(&input, "path", "$.fs.stat path")?;
            let target = broker.workspace_path(path)?;
            let link = std::fs::symlink_metadata(&target)
                .map_err(|e| RpcError::new(CODE_ERROR, format!("$.fs.stat: {path} does not exist ({e})")))?;
            // A link reports what it points at (`resolve: true`); a dangling
            // one is `other`.
            let followed = std::fs::metadata(&target).ok();
            let is_link = link.is_symlink();
            let (kind, size) = match &followed {
                Some(meta) if meta.is_dir() => ("dir", meta.len()),
                Some(meta) if meta.is_file() => ("file", meta.len()),
                Some(meta) => ("other", meta.len()),
                None => ("other", 0),
            };
            Ok(json!({ "kind": kind, "size": size, "mtimeMs": 0, "isLink": is_link }))
        }

        // ---- network ----
        "http.fetch" => op_http_fetch(ctx, &input).await,

        // ---- processes ----
        "process.run" => op_process_run(ctx, &input).await,

        // ---- environment (read-only whitelist; `env.set` is never served) ----
        "env.get" => {
            let name = need_string(&input, "name", "$.env.get name")?;
            Ok(std::env::var(name).map(Value::String).unwrap_or(Value::Null))
        }

        // ---- ui: toasts and friends go to Python ----
        "ui.log" | "ui.toast" | "ui.status" | "ui.notice" | "ui.copy" => {
            let method = if op == "ui.toast" { "mod.toast" } else { "mod.ui" };
            let mut args = input.clone();
            if op != "ui.toast" {
                args["kind"] = json!(op.trim_start_matches("ui."));
            }
            broker.runtime_notify(method, ctx.session.clone(), Some(caller.to_string()), args);
            Ok(Value::Null)
        }

        // ---- panes (design §7.4): the SurfaceTable records the open pane,
        // the runtime relays `mod.pane.open`, and the next render pass draws
        // it through the owning mod's `ui.render` chain ----
        "ui.open" => {
            let id = need_string(&input, "id", "$.ui.open id")?.to_string();
            let title = input.get("title").and_then(Value::as_str).unwrap_or_default().to_string();
            let session = ctx.session.clone().unwrap_or_default();
            broker.surfaces_pane_open(&session, caller, &id, &title).map_err(|e| RpcError::new(CODE_ERROR, e))?;
            broker.runtime_notify(
                "mod.pane.open",
                ctx.session.clone(),
                Some(caller.to_string()),
                json!({ "id": id, "title": title }),
            );
            let broker2 = broker.clone();
            tokio::spawn(async move { broker2.surfaces_refresh(&session).await });
            Ok(json!({ "id": id, "isPlaced": true }))
        }
        "ui.close" => {
            let id = need_string(&input, "id", "$.ui.close id")?;
            let session = ctx.session.clone().unwrap_or_default();
            if let Some(owner) = broker.surfaces_pane_close(&session, id) {
                broker.runtime_notify("mod.pane.close", ctx.session.clone(), Some(owner), json!({ "id": id }));
            }
            let broker2 = broker.clone();
            tokio::spawn(async move { broker2.surfaces_refresh(&session).await });
            Ok(Value::Null)
        }
        "ui.invalidate" => {
            let session = ctx.session.clone().unwrap_or_default();
            let broker2 = broker.clone();
            tokio::spawn(async move { broker2.surfaces_refresh(&session).await });
            Ok(Value::Null)
        }
        "ui.panes" => {
            let session = ctx.session.clone().unwrap_or_default();
            Ok(json!(broker.surfaces_panes(&session)))
        }

        // ---- ui.ask: a question parked here until Python answers it (§7.3) ----
        "ui.ask" => op_ui_ask(ctx, caller, input).await,

        other if UNSERVED_OPS.contains(&other) => Err(no_implementation(other)),
        other if FORWARD_NAMESPACES.contains(&ns_of(other)) => {
            forward_to_runtime(ctx, caller, other, input).await
        }
        other => Err(no_implementation(other)),
    }
}

fn ns_of(op: &str) -> &str {
    op.split('.').next().unwrap_or(op)
}

// ---- $.ui.ask (design §7.3): pending asks live here, between the op that
// raised them and the runtime's `broker.answer` call that resolves them. The
// table is module-global because it is written from two dispatch paths
// (`op_core` and the runtime channel) while `Broker`'s fields are lib-owned.

/// How long a `$.ui.ask` waits for the runtime's answer before failing.
pub const ASK_TIMEOUT_MS: u64 = 60_000;

struct PendingAsk {
    tx: oneshot::Sender<Result<Value, RpcError>>,
}

fn pending_asks() -> &'static Mutex<HashMap<String, PendingAsk>> {
    static ASKS: OnceLock<Mutex<HashMap<String, PendingAsk>>> = OnceLock::new();
    ASKS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn next_ask_id() -> String {
    static SEQ: AtomicU64 = AtomicU64::new(1);
    format!("ask-{}", SEQ.fetch_add(1, Ordering::SeqCst))
}

/// Park a pending ask and return the channel its answer arrives on.
fn register_ask(id: &str) -> oneshot::Receiver<Result<Value, RpcError>> {
    let (tx, rx) = oneshot::channel();
    pending_asks().lock().unwrap().insert(id.to_string(), PendingAsk { tx });
    rx
}

/// Resolve a pending ask with the runtime's answer; `false` when no ask by
/// that id is waiting (already answered, timed out, or never existed).
pub fn resolve_ask(id: &str, value: Value) -> bool {
    match pending_asks().lock().unwrap().remove(id) {
        Some(pending) => {
            let _ = pending.tx.send(Ok(value));
            true
        }
        None => false,
    }
}

/// Fail every pending ask — the runtime went away mid-question (§7.3).
pub fn fail_all_asks(reason: &str) {
    let pending: Vec<PendingAsk> =
        { std::mem::take(&mut *pending_asks().lock().unwrap()) }.into_values().collect();
    for ask in pending {
        let _ = ask.tx.send(Err(RpcError::new(CODE_ERROR, format!("$.ui.ask aborted: {reason}"))));
    }
}

/// Wait for a pending ask's answer, bounded by its timeout. On a timeout the
/// caller (which knows the id) sweeps the entry so a late `broker.answer`
/// names no ask.
async fn wait_ask_answer(rx: oneshot::Receiver<Result<Value, RpcError>>, timeout_ms: u64) -> Result<Value, RpcError> {
    match tokio::time::timeout(std::time::Duration::from_millis(timeout_ms), rx).await {
        Ok(Ok(result)) => result,
        Ok(Err(_)) => Err(RpcError::new(CODE_ERROR, "$.ui.ask was dropped".to_string())),
        Err(_) => Err(RpcError::new(CODE_ERROR, format!("$.ui.ask ran past its {timeout_ms} ms limit"))),
    }
}

/// `$.ui.ask`: validate `{ message, choices? }`, park a pending ask, notify
/// the runtime as `mod.ask`, and settle `{ value }` when Python calls
/// `broker.answer` — or an error on the 60 s cap or a runtime disconnect.
async fn op_ui_ask(ctx: &Arc<ChainCtx>, caller: &str, input: Value) -> Result<Value, RpcError> {
    let message = need_string(&input, "message", "$.ui.ask message")?.to_string();
    let choices = match input.get("choices") {
        None | Some(Value::Null) => None,
        Some(Value::Array(items)) if items.iter().all(Value::is_string) => Some(items.clone()),
        Some(_) => {
            return Err(RpcError::new(CODE_ERROR, "$.ui.ask choices must be an array of strings".to_string()))
        }
    };
    let id = next_ask_id();
    let rx = register_ask(&id);
    let mut payload = json!({ "id": id, "message": message });
    if let Some(choices) = &choices {
        payload["choices"] = json!(choices);
    }
    ctx.broker.runtime_notify("mod.ask", ctx.session.clone(), Some(caller.to_string()), payload);
    let answer = wait_ask_answer(rx, ASK_TIMEOUT_MS).await;
    if answer.is_err() {
        pending_asks().lock().unwrap().remove(&id);
    }
    Ok(json!({ "value": answer? }))
}

fn state_slot(input: &Value) -> Result<String, RpcError> {
    let plugin = input.get("plugin").and_then(Value::as_str);
    let key = input.get("key").and_then(Value::as_str);
    match (plugin, key) {
        (Some(plugin), Some(key)) => Ok(format!("{plugin}\u{0}{key}")),
        _ => Err(RpcError::new(CODE_ERROR, "$.state needs { plugin, key } strings".to_string())),
    }
}

fn ms_field(input: &Value, what: &str) -> Result<u64, RpcError> {
    input
        .get("ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| RpcError::new(CODE_ERROR, format!("{what} needs a non-negative number of milliseconds")))
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// `$.http.fetch`: domain grant already checked; bounded streaming read.
async fn op_http_fetch(ctx: &Arc<ChainCtx>, input: &Value) -> Result<Value, RpcError> {
    let url = need_string(input, "url", "$.http.fetch url")?;
    let init = input.get("init").cloned().unwrap_or(Value::Null);
    let timeout_ms = init.get("timeoutMs").and_then(Value::as_u64).unwrap_or_else(|| {
        ctx.broker.config.read().unwrap().process_timeout_ms
    });
    let client = http_client();
    let method = init.get("method").and_then(Value::as_str).unwrap_or("GET");
    let mut request = client
        .request(
            reqwest::Method::from_bytes(method.as_bytes())
                .map_err(|_| RpcError::new(CODE_ERROR, format!("$.http.fetch: unknown method {method}")))?,
            url,
        )
        .timeout(std::time::Duration::from_millis(timeout_ms.min(300_000)));
    if let Some(headers) = init.get("headers").and_then(Value::as_object) {
        for (name, value) in headers {
            if let Some(value) = value.as_str() {
                request = request.header(name, value);
            }
        }
    }
    if let Some(body) = init.get("body").and_then(Value::as_str) {
        request = request.body(body.to_string());
    }
    let response = request
        .send()
        .await
        .map_err(|e| RpcError::new(CODE_ERROR, format!("$.http.fetch: {e}")))?;
    let status = response.status().as_u16();
    let mut headers = serde_json::Map::new();
    for (name, value) in response.headers() {
        if let Ok(value) = value.to_str() {
            headers.insert(name.as_str().to_string(), json!(value));
        }
    }
    // Bounded read: cancel at the first byte past the cap.
    let mut body: Vec<u8> = Vec::new();
    let mut response = response;
    loop {
        match response.chunk().await {
            Ok(Some(chunk)) => {
                if body.len() + chunk.len() > HTTP_MAX_BYTES as usize {
                    return Err(RpcError::new(
                        CODE_ERROR,
                        format!("$.http.fetch: the response body is larger than {HTTP_MAX_BYTES} bytes"),
                    ));
                }
                body.extend_from_slice(&chunk);
            }
            Ok(None) => break,
            Err(e) => return Err(RpcError::new(CODE_ERROR, format!("$.http.fetch: {e}"))),
        }
    }
    Ok(json!({
        "status": status,
        "ok": (200..300).contains(&status),
        "headers": Value::Object(headers),
        "text": String::from_utf8_lossy(&body).into_owned(),
    }))
}

fn http_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| reqwest::Client::new())
}

/// `$.process.run`: argv, no shell, collected output with caps, process-group
/// kill on timeout (design §15.8).
async fn op_process_run(ctx: &Arc<ChainCtx>, input: &Value) -> Result<Value, RpcError> {
    use std::process::Stdio;
    use tokio::process::Command;

    let config = ctx.broker.config.read().unwrap().clone();
    let argv: Vec<String> = match input.get("argv").and_then(Value::as_array) {
        Some(items) if !items.is_empty() && items.iter().all(Value::is_string) => {
            items.iter().map(|item| item.as_str().unwrap().to_string()).collect()
        }
        _ => {
            return Err(RpcError::new(
                CODE_ERROR,
                "$.process.run needs a non-empty argv list of strings".to_string(),
            ))
        }
    };
    let init = input.get("init").cloned().unwrap_or(Value::Null);
    let timeout_ms = init.get("timeoutMs").and_then(Value::as_u64).unwrap_or(config.process_timeout_ms);
    let timeout_ms = timeout_ms.clamp(1, config.process_max_timeout_ms);
    let mut command = Command::new(&argv[0]);
    command.args(&argv[1..]);
    command.stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null()).kill_on_drop(true);
    // Its own process group, so a timeout can kill the whole tree.
    unsafe {
        command.pre_exec(|| {
            libc::setpgid(0, 0);
            Ok(())
        });
    }
    match init.get("cwd").and_then(Value::as_str) {
        Some(cwd) => command.current_dir(cwd),
        None => command.current_dir(&config.workspace),
    };
    if let Some(env) = init.get("env").and_then(Value::as_object) {
        command.env_clear();
        for (name, value) in env {
            if let Some(value) = value.as_str() {
                command.env(name, value);
            }
        }
    }
    let mut child = command
        .spawn()
        .map_err(|e| RpcError::new(CODE_ERROR, format!("$.process.run: {}: {e}", argv[0])))?;
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    let stdout_task = tokio::spawn(async move { read_capped(&mut stdout).await });
    let stderr_task = tokio::spawn(async move { read_capped(&mut stderr).await });

    let deadline = tokio::time::sleep(std::time::Duration::from_millis(timeout_ms));
    tokio::pin!(deadline);
    let status = tokio::select! {
        status = child.wait() => status.map_err(|e| RpcError::new(CODE_ERROR, format!("$.process.run: {}: {e}", argv[0])))?,
        _ = &mut deadline => {
            // Kill the whole process group, then reap.
            if let Some(pid) = child.id() {
                unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
            }
            let _ = child.wait().await;
            return Err(RpcError::new(
                CODE_ERROR,
                format!("$.process.run: {} did not exit within {timeout_ms} ms", argv[0]),
            ));
        }
    };
    let (stdout_text, _) = stdout_task.await.unwrap_or_default();
    let (stderr_text, _) = stderr_task.await.unwrap_or_default();
    match status.code() {
        Some(code) => Ok(json!({ "exitCode": code, "stdout": stdout_text, "stderr": stderr_text })),
        None => Err(RpcError::new(
            CODE_ERROR,
            format!("$.process.run: {} was terminated by signal {}", argv[0], signal_of(&status)),
        )),
    }
}

fn signal_of(status: &std::process::ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    status.signal().unwrap_or(-1)
}

/// Read a stream to EOF, keeping at most [`PROCESS_OUTPUT_MAX_BYTES`] and
/// draining the rest so the child never blocks on a full pipe.
async fn read_capped(reader: &mut Option<impl tokio::io::AsyncRead + Unpin>) -> (String, usize) {
    let mut kept: Vec<u8> = Vec::new();
    let mut buffer = [0u8; 65_536];
    let Some(reader) = reader else { return (String::new(), 0) };
    loop {
        match reader.read(&mut buffer).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if kept.len() < PROCESS_OUTPUT_MAX_BYTES {
                    let keep = (PROCESS_OUTPUT_MAX_BYTES - kept.len()).min(n);
                    kept.extend_from_slice(&buffer[..keep]);
                }
            }
        }
    }
    (String::from_utf8_lossy(&kept).into_owned(), kept.len())
}

/// Forward a runtime-backed op to Python (design §3.3 right column).
async fn forward_to_runtime(ctx: &Arc<ChainCtx>, caller: &str, op: &str, mut input: Value) -> Result<Value, RpcError> {
    let (ns, method) = op.split_once('.').unwrap_or((op, ""));
    // Mods speak Claude Code tool names; Ginno speaks its own (§15.8).
    if op == "tool.call" {
        if let Some(tool) = input.get("tool").and_then(Value::as_str) {
            input["tool"] = json!(ctx.broker.aliases.to_host(tool));
        }
    }
    let extra = if op == "tool.call" {
        // The pipeline raises `tool.call` for this call from the calling mod,
        // so only mods loaded before it see it.
        json!({ "raisedBy": caller })
    } else {
        Value::Null
    };
    let result = ctx
        .broker
        .runtime_call(ns, method, input, ctx.session.clone(), Some(caller.to_string()), extra)
        .await?;
    if op == "tool.call" {
        if let Some(tool) = result.get("tool").and_then(Value::as_str) {
            let mut result = result.clone();
            result["tool"] = json!(ctx.broker.aliases.to_mod(tool));
            return Ok(result);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unwraps_value_and_deny_answers() {
        assert_eq!(unwrap_op_answer("x.y", &json!({"value": 3})).unwrap(), json!(3));
        assert_eq!(unwrap_op_answer("x.y", &json!({"value": null})).unwrap(), Value::Null);
        let denied = unwrap_op_answer("x.y", &json!({"deny": "no"})).unwrap_err();
        assert_eq!(denied.code, "denied");
        assert_eq!(denied.message, "x.y refused: no");
        assert!(unwrap_op_answer("x.y", &json!({"deny": 3})).is_ok());
        assert!(unwrap_op_answer("x.y", &json!({"other": 1})).is_err());
        assert!(unwrap_op_answer("x.y", &Value::Null).is_err());
    }

    #[test]
    fn no_implementation_names_the_op() {
        let error = no_implementation("audio.play");
        assert_eq!(error.code, "no-implementation");
        assert_eq!(error.message, "no implementation for audio.play");
    }

    // ---- $.ui.ask pending table ----
    // One sequential test: the pending table is process-global, so parallel
    // tests could sweep each other's asks with `fail_all_asks`.

    #[test]
    fn the_ask_table_resolves_times_out_and_fails() {
        let rt = tokio::runtime::Runtime::new().unwrap();

        // A resolve answers the waiting ask exactly once; unknown ids are false.
        let rx = register_ask("ask-rt");
        assert!(resolve_ask("ask-rt", json!("yes")));
        assert_eq!(rx.blocking_recv().unwrap().unwrap(), json!("yes"));
        assert!(!resolve_ask("ask-rt", json!("again")));
        assert!(!resolve_ask("ask-nope", Value::Null));

        // A timeout names the limit, and the caller's sweep makes a late
        // answer name no ask.
        rt.block_on(async {
            let rx = register_ask("ask-timeout");
            let error = wait_ask_answer(rx, 20).await.unwrap_err();
            assert!(error.message.contains("ran past its 20 ms limit"), "{}", error.message);
        });
        pending_asks().lock().unwrap().remove("ask-timeout");
        assert!(!resolve_ask("ask-timeout", json!("late")));

        // A dropped answer (the entry swept elsewhere) is an error, not a hang.
        rt.block_on(async {
            let rx = register_ask("ask-dropped");
            pending_asks().lock().unwrap().remove("ask-dropped");
            let error = wait_ask_answer(rx, 1_000).await.unwrap_err();
            assert_eq!(error.message, "$.ui.ask was dropped");
        });

        // The runtime leaving fails every waiting ask with the reason.
        let first = register_ask("ask-fail-1");
        let second = register_ask("ask-fail-2");
        fail_all_asks("runtime disconnected");
        for rx in [first, second] {
            let error = rx.blocking_recv().unwrap().unwrap_err();
            assert_eq!(error.message, "$.ui.ask aborted: runtime disconnected");
        }
    }
}
