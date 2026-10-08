//! The Python channel (design §5.2, §6.2): a Unix socket next to a 0600 token
//! file. The runtime connects, says `hello{role:"runtime", token, config}`,
//! the broker applies the config (spawning one runner per enabled mod) and
//! answers with the `ready{mods:[...]}` payload. The runtime then raises
//! engine events and answers the broker's forwarded ops; a reconnecting
//! runtime replaces a dead one while runners and mod state stay put.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tokio::io::AsyncBufReadExt;
use tokio::net::UnixListener;
use tokio::net::UnixStream;
use tokio::sync::oneshot;

use crate::protocol::{
    decode_line, result_ok, Frame, FrameWriter, RpcError, CODE_ERROR, CODE_NO_IMPLEMENTATION, CODE_NOT_FOUND,
    PROTOCOL_VERSION,
};
use crate::Broker;

/// Broker↔runtime round-trips are bounded so a wedged runtime cannot hang a
/// mod's `$` call forever.
const FORWARD_TIMEOUT_MS: u64 = 120_000;

/// The connected Python runtime.
pub struct RuntimeConn {
    pub tx: FrameWriter,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, RpcError>>>>,
    next_id: AtomicU64,
}

impl RuntimeConn {
    fn register_pending(&self, id: u64) -> oneshot::Receiver<Result<Value, RpcError>> {
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        rx
    }

    fn fail_pending(&self, error: RpcError) {
        let pending: Vec<oneshot::Sender<Result<Value, RpcError>>> =
            { std::mem::take(&mut *self.pending.lock().unwrap()) }.into_values().collect();
        for tx in pending {
            let _ = tx.send(Err(error.clone()));
        }
    }
}

/// Begin listening. Writes the token file (0600) beside the socket so only
/// the process Python points at the socket can claim the runtime slot.
pub async fn serve(broker: Arc<Broker>, socket_path: PathBuf) -> std::io::Result<()> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let _ = std::fs::remove_file(&socket_path);
    let listener = UnixListener::bind(&socket_path)?;
    restrict_permissions(&socket_path);
    let token = new_token();
    let token_path = token_path_of(&socket_path);
    if let Ok(text) = std::fs::read_to_string(&token_path) {
        if text.trim() == token {
            // Same token survived a restart (same boot): keep it so a runtime
            // holding the old path still authenticates.
        } else {
            write_token(&token_path, &token);
        }
    } else {
        write_token(&token_path, &token);
    }
    broker.report(&format!("listening on {}", socket_path.display()));

    loop {
        match listener.accept().await {
            Ok((stream, _addr)) => {
                let broker = broker.clone();
                let token = token.clone();
                tokio::spawn(async move {
                    if let Err(e) = handle_connection(broker, stream, &token).await {
                        eprintln!("[mod-broker] runtime channel: {e}");
                    }
                });
            }
            Err(e) => {
                broker.report(&format!("socket accept failed: {e}"));
                tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            }
        }
    }
}


fn token_path_of(socket_path: &Path) -> PathBuf {
    let mut name = socket_path.file_name().unwrap_or_default().to_os_string();
    name.push(".token");
    socket_path.with_file_name(name)
}

fn restrict_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

fn write_token(path: &Path, token: &str) {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).write(true).truncate(true).mode(0o600).open(path) {
        let _ = writeln!(file, "{token}");
    }
}

/// A locally unguessable token; the 0600 file keeps it that way.
fn new_token() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let pid = std::process::id() as u128;
    format!("{:x}-{:x}", nanos, pid.rotate_left(32) ^ (&nanos as *const _ as u128))
}

/// Read frames from a fresh connection until it authenticates as the runtime;
/// then serve it as the one runtime channel until it disconnects.
async fn handle_connection(broker: Arc<Broker>, stream: UnixStream, token: &str) -> Result<(), String> {
    let (reader, writer) = stream.into_split();
    let mut lines = tokio::io::BufReader::new(reader).lines();

    let first = lines
        .next_line()
        .await
        .map_err(|e| format!("socket read failed: {e}"))?
        .ok_or_else(|| "socket closed before hello".to_string())?;
    let hello = decode_line(&first)?;
    // Accepted either as `kind:"hello"` or as call{ns:"broker",method:"hello"}
    // (see protocol.rs); token and config ride in the same places either way.
    let (hello_id, got_token, config): (Option<u64>, Option<String>, Value) = match &hello {
        Frame::Hello { id, role, token, config, .. } if role == "runtime" => (*id, token.clone(), config.clone()),
        Frame::Call { id, args, .. } => {
            let token = args.get("token").and_then(Value::as_str).map(str::to_string);
            let config = args.get("config").cloned().unwrap_or(Value::Null);
            (Some(*id), token, config)
        }
        _ => return Err("first frame must be hello{role:\"runtime\", token, config}".to_string()),
    };
    if got_token.as_deref() != Some(token) {
        return Err("runtime hello presented a wrong token".to_string());
    }

    let conn = Arc::new(RuntimeConn {
        tx: FrameWriter::new(writer),
        pending: Mutex::new(HashMap::new()),
        next_id: AtomicU64::new(1_000_000),
    });
    // A reconnecting runtime replaces the old one; runners stay put.
    *broker.runtime.write().unwrap() = Some(conn.clone());

    // Apply the pushed config, then answer ready{mods:[...]}.
    let payload = broker.apply_config(&config).await;
    if let Some(id) = hello_id {
        conn.tx.send(&result_ok(id, payload));
    } else {
        conn.tx.send(&Frame::Notify {
            v: PROTOCOL_VERSION,
            method: "ready".to_string(),
            session: None,
            mod_name: None,
            args: payload,
        });
    }

    loop {
        let line = match lines.next_line().await {
            Ok(Some(line)) => line,
            _ => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let frame = match decode_line(&line) {
            Ok(frame) => frame,
            Err(e) => {
                broker.report(&format!("runtime: bad frame: {e}"));
                continue;
            }
        };
        handle_runtime_frame(&broker, &conn, frame).await;
    }
    // The runtime went away: fail its pending op forwards and every `$.ui.ask`
    // waiting on it (§7.3). Runners keep running and wait for the reconnect
    // (design §5.2).
    conn.fail_pending(RpcError::new(CODE_ERROR, "runtime disconnected"));
    crate::ops::fail_all_asks("runtime disconnected");
    if broker.runtime.read().unwrap().as_ref().is_some_and(|current| Arc::ptr_eq(current, &conn)) {
        // Only clear if no newer connection already replaced us.
        *broker.runtime.write().unwrap() = None;
    }
    Ok(())
}

async fn handle_runtime_frame(broker: &Arc<Broker>, conn: &Arc<RuntimeConn>, frame: Frame) {
    match frame {
        Frame::Event { id, event, session, invocation, payload, deadline_ms, .. } => {
            let broker = broker.clone();
            let conn = conn.clone();
            tokio::spawn(async move {
                let result = broker
                    .raise_event(&event, session, Some(invocation), payload, deadline_ms)
                    .await;
                conn.tx.send_result(id, &result);
            });
        }
        Frame::Result { id, ok, value, code, message, .. } => {
            let res = if ok {
                Ok(value.unwrap_or(Value::Null))
            } else {
                Err(RpcError::new(code.unwrap_or_else(|| "error".into()), message.unwrap_or_default()))
            };
            if let Some(tx) = conn.pending.lock().unwrap().remove(&id) {
                let _ = tx.send(res);
            }
        }
        Frame::Notify { method, args, session, .. } => match method.as_str() {
            "ui.press" => {
                let generation = args.get("generation").and_then(Value::as_u64).unwrap_or(0);
                let action_id = args.get("actionId").and_then(Value::as_str).unwrap_or_default().to_string();
                // Pane Input/Select submissions ride an optional `value`; a
                // plain Button press (or an explicit null) omits it.
                let value = args.get("value").cloned().filter(|v| !v.is_null());
                let session = session.or_else(|| args.get("session").and_then(Value::as_str).map(str::to_string));
                let Some(session) = session else { return };
                let broker = broker.clone();
                tokio::spawn(async move {
                    broker.surfaces_press(&session, generation, &action_id, value).await;
                });
            }
            "mod.answer" => {
                // Asks resolve through the `broker.answer` call, not a notify.
                broker.report("mod.answer ignored: resolve $.ui.ask via call broker.answer");
            }
            "ping" => {}
            other => {
                broker.report(&format!("runtime notify ignored: {other}"));
            }
        },
        Frame::Call { id, ns, method, args, .. } => {
            let result = match (ns.as_str(), method.as_str()) {
                ("broker", "status") => Ok(broker.status_payload().await),
                ("broker", "apply-config") => Ok(broker.apply_config(&args).await),
                // `$.ui.ask`'s answer (§7.3): { id, value } resolves the ask
                // the mod's op parked with the broker.
                ("broker", "answer") => {
                    let ask_id = args.get("id").and_then(Value::as_str).unwrap_or_default().to_string();
                    let value = args.get("value").cloned().unwrap_or(Value::Null);
                    if crate::ops::resolve_ask(&ask_id, value) {
                        Ok(json!({ "resolved": true }))
                    } else {
                        Err(RpcError::new(CODE_NOT_FOUND, format!("no pending ask named {ask_id}")))
                    }
                }
                _ => Err(RpcError::new(CODE_NO_IMPLEMENTATION, format!("no implementation for {ns}.{method}"))),
            };
            conn.tx.send_result(id, &result);
        }
        _ => {}
    }
}

// ---- Broker runtime-channel methods (defined here with the connection) ----
impl Broker {
    /// The connected runtime, if any.
    pub fn runtime_conn(&self) -> Option<Arc<RuntimeConn>> {
        self.runtime.read().unwrap().clone()
    }

    /// Fire-and-forget notify to Python (bands, toasts, status, reports).
    pub fn runtime_notify(&self, method: &str, session: Option<String>, mod_name: Option<String>, args: Value) {
        let frame = Frame::Notify {
            v: PROTOCOL_VERSION,
            method: method.to_string(),
            session,
            mod_name,
            args,
        };
        if let Some(conn) = self.runtime_conn() {
            conn.tx.send(&frame);
        }
    }

    /// Forward a runtime-backed op to Python and await its result.
    pub async fn runtime_call(
        &self,
        ns: &str,
        method: &str,
        args: Value,
        session: Option<String>,
        mod_name: Option<String>,
        extra: Value,
    ) -> Result<Value, RpcError> {
        let conn = self
            .runtime_conn()
            .ok_or_else(|| RpcError::new(CODE_ERROR, "runtime is not connected"))?;
        let id = conn.next_id.fetch_add(1, Ordering::SeqCst);
        let rx = conn.register_pending(id);
        conn.tx.send(&Frame::Call {
            v: PROTOCOL_VERSION,
            id,
            ns: ns.to_string(),
            method: method.to_string(),
            args,
            session,
            mod_name,
            invocation: None,
            extra,
        });
        match tokio::time::timeout(std::time::Duration::from_millis(FORWARD_TIMEOUT_MS), rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) | Err(_) => Err(RpcError::new(CODE_ERROR, "runtime did not answer in time")),
        }
    }
}
