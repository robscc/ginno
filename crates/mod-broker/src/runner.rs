//! Runner supervision (design §3.5): one Node process per mod, spawned as
//! `node <runnerPath> --mod <dir>`, spoken to over stdio newline-JSON frames.
//! Lifecycle: spawn → `hooks-registered` handshake → `start`; heartbeat via
//! the runner's 5 s `ping` (15 s silence = dead); crash restarts back off
//! 1 s/5 s/30 s, then the mod is disabled and Python is notified. A dead
//! runner fails exactly its own hooks — its chains continue.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::sync::{oneshot, Notify};
use tokio::{io::AsyncBufReadExt, process};

use crate::chain::{HubKey, HookMsg};
use crate::protocol::{decode_line, Frame, FrameWriter, RpcError, CODE_RUNNER_DEAD, PROTOCOL_VERSION};
use crate::Broker;

/// Silence longer than this after the last ping marks the runner dead.
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(15);
const HEARTBEAT_CHECK: Duration = Duration::from_secs(5);
/// Handshake must complete within this or the spawn counts as failed.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(20);
const RESTART_BACKOFF_MS: &[u64] = &[1_000, 5_000, 30_000];
/// Generous bound on a single round-trip, so a wedged runner cannot leak calls.
const CALL_TIMEOUT: Duration = Duration::from_secs(300);

/// One live runner connection: the writer half plus the bookkeeping the read
/// loop and the chain share.
pub struct RunnerConn {
    pub name: String,
    /// Connection generation; supervision tasks from older generations exit
    /// instead of acting.
    pub gen: u64,
    pub tx: FrameWriter,
    child: Mutex<Option<process::Child>>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, RpcError>>>>,
    next_id: AtomicU64,
    last_seen: Mutex<Instant>,
    /// Set once `hooks-registered` has been handled.
    handshake_seen: AtomicBool,
    handshake_notify: Mutex<Arc<Notify>>,
}

impl RunnerConn {
    pub fn next_id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::SeqCst)
    }

    pub fn send(&self, frame: &Frame) {
        self.tx.send(frame);
    }

    pub fn send_result(&self, id: u64, res: &Result<Value, RpcError>) {
        self.tx.send_result(id, res);
    }

    fn register_pending(&self, id: u64) -> oneshot::Receiver<Result<Value, RpcError>> {
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        rx
    }

    /// Fail every outstanding round-trip (runner died).
    fn fail_pending(&self, error: RpcError) {
        let pending: Vec<oneshot::Sender<Result<Value, RpcError>>> =
            { std::mem::take(&mut *self.pending.lock().unwrap()) }.into_values().collect();
        for tx in pending {
            let _ = tx.send(Err(error.clone()));
        }
    }

    /// One request-response round-trip with this runner (`$` op, timer fire,
    /// button press).
    pub async fn call(
        &self,
        ns: &str,
        method: &str,
        args: Value,
        session: Option<String>,
        mod_name: Option<String>,
        invocation: Option<String>,
    ) -> Result<Value, RpcError> {
        let id = self.next_id();
        let rx = self.register_pending(id);
        self.send(&Frame::Call {
            v: PROTOCOL_VERSION,
            id,
            ns: ns.to_string(),
            method: method.to_string(),
            args,
            session,
            mod_name,
            invocation,
            extra: Value::Null,
        });
        match tokio::time::timeout(CALL_TIMEOUT, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) | Err(_) => Err(RpcError::new(CODE_RUNNER_DEAD, format!("runner {} did not answer", self.name))),
        }
    }

    fn touch(&self) {
        *self.last_seen.lock().unwrap() = Instant::now();
    }

    fn mark_handshake(&self) {
        self.handshake_seen.store(true, Ordering::SeqCst);
        self.handshake_notify.lock().unwrap().notify_waiters();
    }

    /// Wake a handshake waiter without marking success (connection died).
    fn wake_handshake(&self) {
        self.handshake_notify.lock().unwrap().notify_waiters();
    }

    /// Resolve once the runner reported its hooks; errors when it has not by
    /// the handshake timeout.
    pub async fn wait_handshake(&self) -> Result<(), String> {
        let deadline = tokio::time::sleep(HANDSHAKE_TIMEOUT);
        tokio::pin!(deadline);
        loop {
            if self.handshake_seen.load(Ordering::SeqCst) {
                return Ok(());
            }
            let notify = self.handshake_notify.lock().unwrap().clone();
            tokio::select! {
                _ = &mut deadline => {
                    return Err(format!("runner {} did not report its hooks in time", self.name));
                }
                _ = notify.notified() => {}
            }
        }
    }

    /// Kill the child process (and thereby this connection's streams).
    pub async fn kill(&self) {
        if let Some(child) = self.child.lock().unwrap().as_mut() {
            let _ = child.start_kill();
        }
    }
}

/// Spawn the runner for one mod, wire its read loop and heartbeat monitor,
/// and wait for the handshake. Returns the connection on success.
pub async fn spawn(broker: &Arc<Broker>, name: &str, dir: &str, gen: u64) -> Result<Arc<RunnerConn>, String> {
    let config = broker.config.read().unwrap().clone();
    let log_path = config.logs_dir.join(format!("mod-{name}.log"));
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let log = std::fs::OpenOptions::new().create(true).append(true).open(&log_path)
        .map_err(|e| format!("cannot open {}: {e}", log_path.display()))?;

    let mut command = process::Command::new(&config.node_path);
    command
        .arg("--max-old-space-size=128")
        .arg(&config.runner_path)
        .arg("--mod")
        .arg(dir)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(log)
        .kill_on_drop(true);
    let mut child = command
        .spawn()
        .map_err(|e| format!("cannot spawn node for {name}: {e}"))?;
    let stdin = child.stdin.take().expect("runner stdin piped");
    let stdout = child.stdout.take().expect("runner stdout piped");

    let conn = Arc::new(RunnerConn {
        name: name.to_string(),
        gen,
        tx: FrameWriter::new(stdin),
        child: Mutex::new(Some(child)),
        pending: Mutex::new(HashMap::new()),
        next_id: AtomicU64::new(1_000_000),
        last_seen: Mutex::new(Instant::now()),
        handshake_seen: AtomicBool::new(false),
        handshake_notify: Mutex::new(Arc::new(Notify::new())),
    });

    let read_conn = conn.clone();
    let read_broker = broker.clone();
    let read_name = name.to_string();
    tokio::spawn(async move {
        let mut lines = tokio::io::BufReader::new(stdout).lines();
        loop {
            match lines.next_line().await {
                Ok(Some(line)) => {
                    if line.trim().is_empty() {
                        continue;
                    }
                    let frame = match decode_line(&line) {
                        Ok(frame) => frame,
                        Err(e) => {
                            read_broker.report(&format!("{read_name}: bad frame: {e}"));
                            continue;
                        }
                    };
                    handle_runner_frame(&read_broker, &read_conn, frame).await;
                }
                _ => break,
            }
        }
        on_runner_exit(&read_broker, &read_conn).await;
    });

    let beat_conn = conn.clone();
    let beat_broker = broker.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(HEARTBEAT_CHECK).await;
            let silent_for = beat_conn.last_seen.lock().unwrap().elapsed();
            if silent_for > HEARTBEAT_TIMEOUT {
                beat_broker.report(&format!("{}: runner silent for {} ms; killing", beat_conn.name, silent_for.as_millis()));
                beat_conn.kill().await;
                return;
            }
        }
    });

    // Insert into the runners table so the handshake can be routed.
    broker.runners.write().await.insert(name.to_string(), conn.clone());
    Ok(conn)
}

/// Route one inbound frame from a runner.
async fn handle_runner_frame(broker: &Arc<Broker>, conn: &Arc<RunnerConn>, frame: Frame) {
    conn.touch();
    match frame {
        Frame::Result { id, ok, value, code, message, .. } => {
            let res = if ok {
                Ok(value.unwrap_or(Value::Null))
            } else {
                Err(RpcError::new(code.unwrap_or_else(|| "error".into()), message.unwrap_or_default()))
            };
            if let Some(tx) = conn.pending.lock().unwrap().remove(&id) {
                let _ = tx.send(res);
                return;
            }
            // A reply to an `event`/`catch-call` frame → the active hook run.
            broker.hub.send(
                &HubKey::Reply { mod_name: conn.name.clone(), frame_id: id },
                HookMsg::Reply { res },
            );
        }
        Frame::Next { id, invocation, e, .. } => {
            // The runner's own frame id: the beneath result is replied to it.
            broker.hub.send(
                &HubKey::Active { invocation, mod_name: conn.name.clone() },
                HookMsg::Next { frame_id: id, e },
            );
        }
        Frame::Call { id, ns, method, args, session, mod_name, invocation, .. } => {
            let broker = broker.clone();
            let conn = conn.clone();
            tokio::spawn(async move {
                handle_runner_call(&broker, &conn, id, ns, method, args, session, mod_name, invocation).await;
            });
        }
        Frame::Notify { method, args, session, mod_name, .. } => match method.as_str() {
            "ping" => {}
            _ => {
                // Unknown notifications are forwarded to Python with the mod named.
                broker.runtime_notify(&method, session, mod_name.or_else(|| Some(conn.name.clone())), args);
            }
        },
        _ => {}
    }
}

/// A runner's `call` frames: the `hooks-registered` handshake, or a `$` op.
/// A `$` call made under an invocation pauses that hook's budget while the
/// broker works — except `clock.sleep`, which counts as the hook's own time.
#[allow(clippy::too_many_arguments)]
async fn handle_runner_call(
    broker: &Arc<Broker>,
    conn: &Arc<RunnerConn>,
    id: u64,
    ns: String,
    method: String,
    args: Value,
    session: Option<String>,
    mod_name: Option<String>,
    invocation: Option<String>,
) {
    if ns == "runner" && method == "loaded" {
        // The deployment config for this mod — `settings.json`'s
        // `mods.items.<name>.config` as the runtime pushed it. The runner
        // asks before registering and merges it over the mod's own
        // `userConfig` defaults (which ride the args back for the report).
        let config = broker
            .config
            .read()
            .unwrap()
            .mods
            .iter()
            .find(|spec| spec.name == conn.name)
            .map(|spec| spec.config.clone())
            .filter(|value| value.is_object())
            .unwrap_or_else(|| Value::Object(serde_json::Map::new()));
        conn.send_result(id, &Ok(serde_json::json!({ "config": config })));
        return;
    }
    if ns == "runner" && method == "hooks-registered" {
        match broker.register_mod_hooks(&conn.name, &args).await {
            Ok(()) => {
                conn.mark_handshake();
                broker.notify_mod_status(&conn.name, "loaded");
                conn.send_result(id, &Ok(Value::String("start".to_string())));
            }
            Err(e) => {
                conn.mark_handshake();
                conn.send_result(id, &Err(RpcError::new("error", e)));
            }
        }
        return;
    }
    let op = format!("{ns}.{method}");
    let caller = mod_name.unwrap_or_else(|| conn.name.clone());
    let pauses_clock = invocation.is_some() && op != "clock.sleep";
    let budget = if pauses_clock {
        let invocation = invocation.clone().unwrap_or_default();
        broker.budgets.lock().unwrap().get(&(invocation, caller.clone())).cloned()
    } else {
        None
    };
    if let Some(budget) = &budget {
        budget.pause();
    }
    let result = broker
        .invoke_op(&caller, &op, session, args, invocation, 0)
        .await;
    if let Some(budget) = &budget {
        budget.resume();
    }
    // A fire-and-forget style failure (e.g. `$.ui.log`) is still reported.
    if let Err(e) = &result {
        if e.code == crate::protocol::CODE_NO_IMPLEMENTATION {
            broker.report(&format!("{}: $.{op} failed: {}", conn.name, e.message));
        }
    }
    conn.send_result(id, &result);
}

/// The runner's streams closed: fail its pending calls, fail every hook it
/// had in flight (kind `throw`, "runner dead" — the chains continue), and
/// restart with backoff unless the mod is going away.
async fn on_runner_exit(broker: &Arc<Broker>, conn: &Arc<RunnerConn>) {
    conn.fail_pending(RpcError::new(CODE_RUNNER_DEAD, format!("runner {} died", conn.name)));
    conn.wake_handshake();
    broker.hub.send_to_mod(&conn.name, HookMsg::RunnerDead);

    // Only the current generation's supervisor restarts; a newer one exists.
    let current = broker.runners.read().await.get(&conn.name).map(|c| c.gen);
    if current != Some(conn.gen) {
        return;
    }
    if broker.mod_unloading(&conn.name) {
        return;
    }
    broker.notify_mod_status(&conn.name, "restarting");
    let attempt = broker.bump_restart_count(&conn.name);
    match RESTART_BACKOFF_MS.get(attempt.saturating_sub(1) as usize) {
        Some(&ms) => {
            broker.report(&format!("{}: runner exited; restarting in {ms} ms (attempt {attempt})", conn.name));
            tokio::time::sleep(Duration::from_millis(ms)).await;
            let current = broker.runners.read().await.get(&conn.name).map(|c| c.gen);
            if current != Some(conn.gen) || broker.mod_unloading(&conn.name) {
                return;
            }
            let gen = broker.next_runner_gen(&conn.name);
            if let Err(e) = load(broker, &conn.name, gen).await {
                broker.report(&format!("{}: restart failed: {e}", conn.name));
            }
        }
        None => {
            // Three strikes: disable the mod and tell Python (→ settings page).
            broker.disable_mod(&conn.name).await;
        }
    }
}

/// Spawn one configured mod's runner and wait for its handshake. On success
/// the mod's hooks are registered (the handshake handler did it); on failure
/// the connection is cleaned up and the error names the cause. Boxed so the
/// supervision cycle (spawn → exit → load → spawn) stays a finite type.
pub fn load(
    broker: &Arc<Broker>,
    name: &str,
    gen: u64,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send>> {
    let broker = Arc::clone(broker);
    let name = name.to_string();
    Box::pin(async move {
    let spec = broker.mod_spec(&name).ok_or_else(|| format!("{name}: no longer configured"))?;
    let conn = match spawn(&broker, &name, &spec.dir, gen).await {
        Ok(conn) => conn,
        Err(e) => {
            broker.notify_mod_status(&name, "error");
            return Err(e);
        }
    };
    let outcome = conn.wait_handshake().await;
    if outcome.is_err() {
        // Clean up so a later config apply can try again.
        let current = broker.runners.read().await.get(&name).map(|c| c.gen);
        if current == Some(gen) {
            broker.runners.write().await.remove(&name);
        }
        conn.kill().await;
        broker.notify_mod_status(&name, "error");
    }
    outcome
    })
}
