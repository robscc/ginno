//! Shared harness for the mod-broker integration tests: broker + Unix
//! socket + fake runtime client (the test plays the Python runtime), the
//! `needs_node!` skip guard, and the per-fixture `Env` builder.
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, Notify};

use ginno_mod_broker::Broker;

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------

static ENV_SEQ: AtomicU64 = AtomicU64::new(0);

pub(crate) fn node_bin() -> String {
    std::env::var("NODE_BIN").unwrap_or_else(|_| "node".to_string())
}

/// Integration tests spawn real Node runners; without node (or with NODE_BIN
/// pointing nowhere) they skip instead of failing.
pub(crate) fn node_available() -> bool {
    let node = node_bin();
    if std::path::Path::new(&node).exists() {
        return true;
    }
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|dir| dir.join(&node).is_file()))
        .unwrap_or(false)
}

macro_rules! needs_node {
    () => {
        if !crate::common::node_available() {
            eprintln!("skipping: no node binary (set NODE_BIN to run integration tests)");
            return;
        }
    };
}

fn mock_runner_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mock-runner.mjs")
}

struct ClientInner {
    writer: Arc<mpsc::UnboundedSender<String>>,
    inbox: std::sync::Mutex<Vec<Value>>,
    wake: Notify,
    /// Ops the fake runtime serves (`session.usage` and friends); everything
    /// else is refused like an unimplemented Python backend.
    responder: std::sync::Mutex<Option<Box<dyn Fn(&str, &str) -> Option<Value> + Send>>>,
    /// Set to make both pump tasks drop the socket: the broker then sees the
    /// runtime disconnect (the `$.ui.ask` abort test).
    shutdown: Notify,
}

/// The test's stand-in for Python's ModChannel: a framed socket client.
pub(crate) struct RuntimeClient {
    inner: Arc<ClientInner>,
    next_id: AtomicU64,
    token: String,
}

impl RuntimeClient {
    async fn connect(socket: &Path) -> RuntimeClient {
        let stream = UnixStream::connect(socket)
            .await
            .expect("connect to broker socket");
        let (reader, mut socket_writer) = stream.into_split();
        let (tx, mut rx) = mpsc::unbounded_channel::<String>();
        let inner = Arc::new(ClientInner {
            writer: Arc::new(tx),
            inbox: std::sync::Mutex::new(Vec::new()),
            wake: Notify::new(),
            responder: std::sync::Mutex::new(None),
            shutdown: Notify::new(),
        });
        // Outbound frames: from `send`/`call_broker` into the socket.
        let writer_pump = Arc::clone(&inner);
        tokio::spawn(async move {
            loop {
                let line = tokio::select! {
                    _ = writer_pump.shutdown.notified() => break,
                    line = rx.recv() => line,
                };
                let Some(line) = line else { break };
                if socket_writer.write_all(line.as_bytes()).await.is_err() {
                    break;
                }
            }
        });
        // Inbound frames; forwarded ops are answered like the Python runtime
        // would (the registered responder serves what it knows).
        let pump = Arc::clone(&inner);
        let inner2 = Arc::clone(&inner);
        let replies = Arc::clone(&inner.writer);
        tokio::spawn(async move {
            let mut lines = tokio::io::BufReader::new(reader).lines();
            loop {
                let line = tokio::select! {
                    _ = pump.shutdown.notified() => break,
                    line = lines.next_line() => line,
                };
                let Ok(Some(line)) = line else { break };
                if let Ok(frame) = serde_json::from_str::<Value>(&line) {
                    if frame["kind"] == "call" {
                        let ns = frame["ns"].as_str().unwrap_or_default().to_string();
                        let method = frame["method"].as_str().unwrap_or_default().to_string();
                        let served = inner2
                            .responder
                            .lock()
                            .unwrap()
                            .as_ref()
                            .and_then(|serve| serve(&ns, &method));
                        let reply = match served {
                            Some(value) => json!({
                                "v": 1, "kind": "result", "id": frame["id"], "ok": true, "value": value,
                            }),
                            None => json!({
                                "v": 1, "kind": "result", "id": frame["id"],
                                "ok": false, "code": "not-found",
                                "message": "test runtime serves no ops",
                            }),
                        };
                        let mut line = reply.to_string();
                        line.push('\n');
                        let _ = replies.send(line);
                    }
                    pump.inbox.lock().unwrap().push(frame);
                    pump.wake.notify_waiters();
                }
            }
        });
        RuntimeClient {
            inner,
            next_id: AtomicU64::new(100),
            token: String::new(),
        }
    }

    fn send(&self, frame: &Value) {
        let mut line = frame.to_string();
        line.push('\n');
        self.inner.writer.send(line).expect("broker socket closed");
    }

    /// Take the first inbound frame matching `pred`, if any.
    pub(crate) fn take(&self, pred: impl Fn(&Value) -> bool) -> Option<Value> {
        let mut inbox = self.inner.inbox.lock().unwrap();
        let position = inbox.iter().position(|frame| {
            frame
                .get("kind")
                .and_then(Value::as_str)
                .is_some_and(|kind| kind != "result" && pred(frame))
        });
        position.map(|position| inbox.remove(position))
    }

    async fn wait_frame(
        &self,
        what: &str,
        pred: impl Fn(&Value) -> bool,
        timeout: Duration,
    ) -> Value {
        let start = Instant::now();
        loop {
            if let Some(frame) = self.take(&pred) {
                return frame;
            }
            assert!(start.elapsed() < timeout, "timed out waiting for {what}");
            tokio::select! {
                _ = self.inner.wake.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(30)) => {}
            }
        }
    }

    pub(crate) async fn wait_result(
        &self,
        id: u64,
        what: &str,
        timeout: Duration,
    ) -> Result<Value, String> {
        let start = Instant::now();
        loop {
            {
                let mut inbox = self.inner.inbox.lock().unwrap();
                if let Some(position) = inbox.iter().position(|frame| {
                    frame.get("kind").and_then(Value::as_str) == Some("result")
                        && frame.get("id").and_then(Value::as_u64) == Some(id)
                }) {
                    let frame = inbox.remove(position);
                    if frame["ok"].as_bool() == Some(true) {
                        return Ok(frame.get("value").cloned().unwrap_or(Value::Null));
                    }
                    return Err(format!(
                        "{}: {}",
                        frame["code"].as_str().unwrap_or("?"),
                        frame["message"].as_str().unwrap_or("?")
                    ));
                }
            }
            assert!(
                start.elapsed() < timeout,
                "timed out waiting for result of {what}"
            );
            tokio::select! {
                _ = self.inner.wake.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(30)) => {}
            }
        }
    }

    /// Send the hello handshake and return the ready payload.
    pub(crate) async fn hello(&self, config: &Value) -> Value {
        self.send(&json!({"v": 1, "kind": "hello", "id": 1, "role": "runtime", "token": self.token.clone(), "config": config}));
        let ready = self
            .wait_result(1, "hello/ready", Duration::from_secs(120))
            .await
            .expect("ready");
        eprintln!("ready payload: {ready}");
        ready
    }

    /// Raise an engine event without waiting for its result; returns the
    /// frame id for [`Self::wait_result`] (for events that only settle once
    /// the test has answered something in between, like `$.ui.ask`).
    pub(crate) fn send_event(&self, event: &str, session: Option<&str>, payload: Value) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut frame = json!({
            "v": 1, "kind": "event", "id": id, "event": event,
            "invocation": format!("it-{id}"), "payload": payload,
        });
        if let Some(session) = session {
            frame["session"] = json!(session);
        }
        self.send(&frame);
        id
    }

    /// Call a broker-side method (`broker.answer`); returns the frame id.
    pub(crate) fn call_broker(&self, method: &str, args: Value) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        self.send(&json!({"v": 1, "kind": "call", "id": id, "ns": "broker", "method": method, "args": args}));
        id
    }

    /// Close the socket: the broker sees the runtime disconnect.
    pub(crate) async fn disconnect(&self) {
        self.inner.shutdown.notify_waiters();
        // Give both pump tasks a beat to drop the stream halves.
        tokio::time::sleep(Duration::from_millis(150)).await;
    }

    /// Push a new config on the live connection (`broker.apply-config`).
    pub(crate) async fn apply_config(&self, config: &Value) -> Value {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        self.send(&json!({"v": 1, "kind": "call", "id": id, "ns": "broker", "method": "apply-config", "args": config}));
        self.wait_result(id, "apply-config", Duration::from_secs(120))
            .await
            .expect("apply-config reply")
    }

    /// Raise an engine event and return the chain's final value.
    pub(crate) async fn raise(&self, event: &str, session: Option<&str>, payload: Value) -> Value {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut frame = json!({
            "v": 1, "kind": "event", "id": id, "event": event,
            "invocation": format!("it-{id}"), "payload": payload,
        });
        if let Some(session) = session {
            frame["session"] = json!(session);
        }
        self.send(&frame);
        self.wait_result(id, event, Duration::from_secs(60))
            .await
            .expect("event result")
    }

    /// Register which forwarded ops the fake runtime serves.
    pub(crate) fn push_responder(
        &self,
        serve: impl Fn(&str, &str) -> Option<Value> + Send + 'static,
    ) {
        *self.inner.responder.lock().unwrap() = Some(Box::new(serve));
    }

    pub(crate) fn notify(&self, method: &str, args: Value, session: Option<&str>) {
        let mut frame = json!({"v": 1, "kind": "notify", "method": method, "args": args});
        if let Some(session) = session {
            frame["session"] = json!(session);
        }
        self.send(&frame);
    }

    pub(crate) async fn wait_notify(&self, method: &str, timeout: Duration) -> Value {
        self.wait_frame(
            &format!("notify {method}"),
            |frame| frame.get("method").and_then(Value::as_str) == Some(method),
            timeout,
        )
        .await
    }

    pub(crate) async fn wait_report_containing(&self, needle: &str, timeout: Duration) -> String {
        let start = Instant::now();
        loop {
            if let Some(frame) = self.take(|frame| {
                frame.get("method").and_then(Value::as_str) == Some("mod.report")
                    && frame
                        .pointer("/args/line")
                        .and_then(Value::as_str)
                        .is_some_and(|line| line.contains(needle))
            }) {
                return frame
                    .pointer("/args/line")
                    .and_then(Value::as_str)
                    .unwrap()
                    .to_string();
            }
            assert!(
                start.elapsed() < timeout,
                "timed out waiting for report containing {needle:?}"
            );
            tokio::select! {
                _ = self.inner.wake.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(30)) => {}
            }
        }
    }
}

pub(crate) struct Env {
    pub(crate) dir: PathBuf,
    pub(crate) broker: Arc<Broker>,
    pub(crate) runtime: RuntimeClient,
}

impl Env {
    /// Start a broker + socket + fake runtime client. Mods are declared later
    /// via [`Env::hello_with`]; their fixture dirs live under `<dir>/mods`.
    pub(crate) async fn new(name: &str) -> Env {
        let seq = ENV_SEQ.fetch_add(1, Ordering::SeqCst);
        let dir =
            std::env::temp_dir().join(format!("mod-broker-it-{}-{seq}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("workspace")).unwrap();
        std::fs::create_dir_all(dir.join("mods")).unwrap();
        let socket = dir.join("broker.sock");
        let broker = Broker::new(Some(dir.join("store.json")));
        let serve_broker = Arc::clone(&broker);
        let serve_socket = socket.clone();
        tokio::spawn(async move {
            let _ = ginno_mod_broker::runtime::serve(serve_broker, serve_socket).await;
        });
        // serve() writes the token file before it starts accepting.
        let token_path = dir.join("broker.sock.token");
        let start = Instant::now();
        while !token_path.exists() {
            assert!(
                start.elapsed() < Duration::from_secs(10),
                "token file never appeared"
            );
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
        let token = std::fs::read_to_string(&token_path)
            .unwrap()
            .trim()
            .to_string();
        let mut runtime = RuntimeClient::connect(&socket).await;
        runtime.token = token;
        Env {
            dir,
            broker,
            runtime,
        }
    }

    fn mod_dir(&self, name: &str) -> PathBuf {
        self.dir.join("mods").join(name)
    }

    /// Write one fixture mod's scenario.
    pub(crate) fn scenario(&self, name: &str, hooks: Value) {
        let dir = self.mod_dir(name);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("scenario.json"),
            serde_json::to_string(&hooks).unwrap(),
        )
        .unwrap();
    }

    pub(crate) fn spec(&self, name: &str, grants: Value) -> Value {
        json!({
            "enabled": true,
            "dir": self.mod_dir(name),
            "grants": grants,
            "config": {},
        })
    }

    pub(crate) fn config(&self, items: Value, budgets: Value) -> Value {
        json!({
            "nodePath": node_bin(),
            "runnerPath": mock_runner_path(),
            "workspace": self.dir.join("workspace"),
            "storePath": self.dir.join("store.json"),
            "logsDir": self.dir.join("logs"),
            "mods": { "items": items },
            "budgets": budgets,
        })
    }

    pub(crate) async fn hello_with(&self, items: Value, budgets: Value) -> Value {
        self.runtime.hello(&self.config(items, budgets)).await
    }

    pub(crate) fn trace(&self, name: &str) -> String {
        std::fs::read_to_string(self.mod_dir(name).join("trace.log")).unwrap_or_default()
    }

    /// Wait until the mod's trace contains `needle`.
    pub(crate) async fn wait_trace(&self, name: &str, needle: &str) {
        let start = Instant::now();
        loop {
            if self.trace(name).contains(needle) {
                return;
            }
            assert!(
                start.elapsed() < Duration::from_secs(15),
                "trace of {name} never contained {needle:?}; got: {:?}",
                self.trace(name)
            );
            tokio::time::sleep(Duration::from_millis(40)).await;
        }
    }

    pub(crate) fn trace_count(&self, name: &str, needle: &str) -> usize {
        self.trace(name).matches(needle).count()
    }

    pub(crate) fn write_workspace(&self, relative: &str, text: &str) {
        let path = self.dir.join("workspace").join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, text).unwrap();
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        // Best-effort cleanup; the runner children die with the broker task.
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

pub(crate) fn default_budgets() -> Value {
    json!({})
}
