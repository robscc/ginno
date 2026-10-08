//! Protocol-level integration tests: a real broker on a real Unix socket,
//! mock Node runners (`tests/fixtures/mock-runner.mjs`) driven by per-fixture
//! `scenario.json` files, and the test itself playing the Python runtime —
//! raising engine events, receiving bands/toasts/reports. These are the port
//! of DSH's chain.spec / matcher.spec semantics to the distributed broker
//! (design §12 P0 test row); matcher and BudgetClock unit cases live beside
//! those modules.

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

fn node_bin() -> String {
    std::env::var("NODE_BIN").unwrap_or_else(|_| "node".to_string())
}

/// Integration tests spawn real Node runners; without node (or with NODE_BIN
/// pointing nowhere) they skip instead of failing.
fn node_available() -> bool {
    let node = node_bin();
    if std::path::Path::new(&node).exists() {
        return true;
    }
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths).any(|dir| dir.join(&node).is_file())
        })
        .unwrap_or(false)
}

macro_rules! needs_node {
    () => {
        if !node_available() {
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
struct RuntimeClient {
    inner: Arc<ClientInner>,
    next_id: AtomicU64,
    token: String,
}

impl RuntimeClient {
    async fn connect(socket: &Path) -> RuntimeClient {
        let stream = UnixStream::connect(socket).await.expect("connect to broker socket");
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
                        let served = inner2.responder.lock().unwrap().as_ref().and_then(|serve| serve(&ns, &method));
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
        RuntimeClient { inner, next_id: AtomicU64::new(100), token: String::new() }
    }

    fn send(&self, frame: &Value) {
        let mut line = frame.to_string();
        line.push('\n');
        self.inner.writer.send(line).expect("broker socket closed");
    }

    /// Take the first inbound frame matching `pred`, if any.
    fn take(&self, pred: impl Fn(&Value) -> bool) -> Option<Value> {
        let mut inbox = self.inner.inbox.lock().unwrap();
        let position = inbox.iter().position(|frame| frame.get("kind").and_then(Value::as_str).is_some_and(|kind| kind != "result" && pred(frame)));
        position.map(|position| inbox.remove(position))
    }

    async fn wait_frame(&self, what: &str, pred: impl Fn(&Value) -> bool, timeout: Duration) -> Value {
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

    async fn wait_result(&self, id: u64, what: &str, timeout: Duration) -> Result<Value, String> {
        let start = Instant::now();
        loop {
            {
                let mut inbox = self.inner.inbox.lock().unwrap();
                if let Some(position) = inbox.iter().position(|frame| {
                    frame.get("kind").and_then(Value::as_str) == Some("result") && frame.get("id").and_then(Value::as_u64) == Some(id)
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
            assert!(start.elapsed() < timeout, "timed out waiting for result of {what}");
            tokio::select! {
                _ = self.inner.wake.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(30)) => {}
            }
        }
    }

    /// Send the hello handshake and return the ready payload.
    async fn hello(&self, config: &Value) -> Value {
        self.send(&json!({"v": 1, "kind": "hello", "id": 1, "role": "runtime", "token": self.token.clone(), "config": config}));
        let ready = self.wait_result(1, "hello/ready", Duration::from_secs(120)).await.expect("ready");
        eprintln!("ready payload: {ready}");
        ready
    }

    /// Raise an engine event without waiting for its result; returns the
    /// frame id for [`Self::wait_result`] (for events that only settle once
    /// the test has answered something in between, like `$.ui.ask`).
    fn send_event(&self, event: &str, session: Option<&str>, payload: Value) -> u64 {
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
    fn call_broker(&self, method: &str, args: Value) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        self.send(&json!({"v": 1, "kind": "call", "id": id, "ns": "broker", "method": method, "args": args}));
        id
    }

    /// Close the socket: the broker sees the runtime disconnect.
    async fn disconnect(&self) {
        self.inner.shutdown.notify_waiters();
        // Give both pump tasks a beat to drop the stream halves.
        tokio::time::sleep(Duration::from_millis(150)).await;
    }

    /// Push a new config on the live connection (`broker.apply-config`).
    async fn apply_config(&self, config: &Value) -> Value {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        self.send(&json!({"v": 1, "kind": "call", "id": id, "ns": "broker", "method": "apply-config", "args": config}));
        self.wait_result(id, "apply-config", Duration::from_secs(120)).await.expect("apply-config reply")
    }

    /// Raise an engine event and return the chain's final value.
    async fn raise(&self, event: &str, session: Option<&str>, payload: Value) -> Value {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut frame = json!({
            "v": 1, "kind": "event", "id": id, "event": event,
            "invocation": format!("it-{id}"), "payload": payload,
        });
        if let Some(session) = session {
            frame["session"] = json!(session);
        }
        self.send(&frame);
        self.wait_result(id, event, Duration::from_secs(60)).await.expect("event result")
    }

    /// Register which forwarded ops the fake runtime serves.
    fn push_responder(&self, serve: impl Fn(&str, &str) -> Option<Value> + Send + 'static) {
        *self.inner.responder.lock().unwrap() = Some(Box::new(serve));
    }

    fn notify(&self, method: &str, args: Value, session: Option<&str>) {
        let mut frame = json!({"v": 1, "kind": "notify", "method": method, "args": args});
        if let Some(session) = session {
            frame["session"] = json!(session);
        }
        self.send(&frame);
    }

    async fn wait_notify(&self, method: &str, timeout: Duration) -> Value {
        self.wait_frame(
            &format!("notify {method}"),
            |frame| frame.get("method").and_then(Value::as_str) == Some(method),
            timeout,
        )
        .await
    }

    async fn wait_report_containing(&self, needle: &str, timeout: Duration) -> String {
        let start = Instant::now();
        loop {
            if let Some(frame) = self.take(|frame| {
                frame.get("method").and_then(Value::as_str) == Some("mod.report")
                    && frame.pointer("/args/line").and_then(Value::as_str).is_some_and(|line| line.contains(needle))
            }) {
                return frame.pointer("/args/line").and_then(Value::as_str).unwrap().to_string();
            }
            assert!(start.elapsed() < timeout, "timed out waiting for report containing {needle:?}");
            tokio::select! {
                _ = self.inner.wake.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(30)) => {}
            }
        }
    }
}

struct Env {
    dir: PathBuf,
    broker: Arc<Broker>,
    runtime: RuntimeClient,
}

impl Env {
    /// Start a broker + socket + fake runtime client. Mods are declared later
    /// via [`Env::hello_with`]; their fixture dirs live under `<dir>/mods`.
    async fn new(name: &str) -> Env {
        let seq = ENV_SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("mod-broker-it-{}-{seq}-{name}", std::process::id()));
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
            assert!(start.elapsed() < Duration::from_secs(10), "token file never appeared");
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
        let token = std::fs::read_to_string(&token_path).unwrap().trim().to_string();
        let mut runtime = RuntimeClient::connect(&socket).await;
        runtime.token = token;
        Env { dir, broker, runtime }
    }

    fn mod_dir(&self, name: &str) -> PathBuf {
        self.dir.join("mods").join(name)
    }

    /// Write one fixture mod's scenario.
    fn scenario(&self, name: &str, hooks: Value) {
        let dir = self.mod_dir(name);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("scenario.json"), serde_json::to_string(&hooks).unwrap()).unwrap();
    }

    fn spec(&self, name: &str, grants: Value) -> Value {
        json!({
            "enabled": true,
            "dir": self.mod_dir(name),
            "grants": grants,
            "config": {},
        })
    }

    fn config(&self, items: Value, budgets: Value) -> Value {
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

    async fn hello_with(&self, items: Value, budgets: Value) -> Value {
        self.runtime.hello(&self.config(items, budgets)).await
    }

    fn trace(&self, name: &str) -> String {
        std::fs::read_to_string(self.mod_dir(name).join("trace.log")).unwrap_or_default()
    }

    /// Wait until the mod's trace contains `needle`.
    async fn wait_trace(&self, name: &str, needle: &str) {
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

    fn trace_count(&self, name: &str, needle: &str) -> usize {
        self.trace(name).matches(needle).count()
    }

    fn write_workspace(&self, relative: &str, text: &str) {
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

fn default_budgets() -> Value { json!({}) }

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn handshake_reports_loaded_mods_and_hooks() {
    needs_node!();
    let env = Env::new("handshake").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call" }, { "event": "turn.complete", "matcher": { "tool": "Write" } }] }));
    let ready = env.hello_with(json!({ "a": env.spec("a", json!({})) }), default_budgets()).await;
    let first = &ready["mods"][0];
    assert_eq!(first["name"], "a");
    assert_eq!(first["status"], "loaded");
    assert!(first["hooks"].as_str().unwrap().contains("tool.call"));
    assert!(first["hooks"].as_str().unwrap().contains("turn.complete{tool=Write}"));
}

// DSH chain.spec: "runs hooks outermost first and the core last, with
// matchers evaluated against the input each hook receives".
#[tokio::test(flavor = "multi_thread")]
async fn chain_order_and_matchers_reevaluate_per_hook() {
    needs_node!();
    let env = Env::new("chain-order").await;
    // DSH's "rewrites" spec case lives on prompt.submit: matchers re-evaluate
    // against the input each hook receives. (On tool.call this rewrite would
    // be refused — see tool_call_reroutes_are_refused.)
    env.scenario("a", json!({ "hooks": [{ "event": "prompt.submit", "behavior": "next", "ePatch": { "text": "trimmed" } }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "prompt.submit", "matcher": { "text": "  raw  " }, "behavior": "answer", "answer": { "seen": "b" } }] }));
    env.scenario("c", json!({ "hooks": [{ "event": "prompt.submit", "matcher": { "text": ["trimmed", "x"] }, "behavior": "answer", "answer": { "seen": "c" } }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})), "c": env.spec("c", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("prompt.submit", Some("s1"), json!({ "text": "  raw  " })).await;
    // a rewrote to "trimmed"; b's matcher no longer matches the input it
    // would receive; c's does. The chain settles with c's answer.
    assert_eq!(settled, json!({ "seen": "c" }));
    env.wait_trace("a", "a:prompt.submit:begin").await;
    assert!(env.trace("c").contains("c:prompt.submit:begin"));
    assert!(!env.trace("b").contains("prompt.submit"), "b must be skipped once the input changed");
}

// DSH chain.spec: "hands a second next call the first run" + eager answer.
#[tokio::test(flavor = "multi_thread")]
async fn next_runs_once_and_eager_answer_still_waits_for_beneath() {
    needs_node!();
    let env = Env::new("next-once").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "next_twice" }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "same": true, "beneath": { "ran": "b" } }));
    env.wait_trace("a", "same=true").await;
    // The beneath hook ran exactly once.
    assert_eq!(env.trace_count("b", "b:tool.call:begin"), 1);
}

// DSH chain.spec: skip rules — throw before next, fail after next, no result.
#[tokio::test(flavor = "multi_thread")]
async fn skip_rules_throw_after_next_and_no_result() {
    needs_node!();
    let env = Env::new("skip-rules").await;
    env.scenario("throws", json!({ "hooks": [{ "event": "tool.call", "behavior": "throw", "message": "boom" }] }));
    env.scenario("late", json!({ "hooks": [{ "event": "tool.call", "behavior": "next_then_throw", "message": "after" }] }));
    env.scenario("void", json!({ "hooks": [{ "event": "tool.call", "behavior": "no_result" }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));

    // throw before next: the next hook runs in its place.
    let items = json!({ "throws": env.spec("throws", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;
    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    env.runtime.wait_report_containing("throws: tool.call hook skipped: threw boom", Duration::from_secs(10)).await;

    // Fail after next resolved: the result from beneath is kept, nothing reruns.
    let env2 = Env::new("skip-rules-late").await;
    env2.scenario("late", json!({ "hooks": [{ "event": "tool.call", "behavior": "next_then_throw", "message": "after" }] }));
    env2.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "late": env2.spec("late", json!({})), "b": env2.spec("b", json!({})) });
    let _ready = env2.hello_with(items, default_budgets()).await;
    let settled = env2.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    env2.runtime.wait_report_containing("late: tool.call hook skipped: threw after", Duration::from_secs(10)).await;
    assert_eq!(env2.trace_count("b", "b:tool.call:begin"), 1);

    // A hook settling with no result is skipped the same way.
    let env3 = Env::new("skip-rules-noresult").await;
    env3.scenario("void", json!({ "hooks": [{ "event": "tool.call", "behavior": "no_result" }] }));
    env3.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "void": env3.spec("void", json!({})), "b": env3.spec("b", json!({})) });
    let _ready = env3.hello_with(items, default_budgets()).await;
    let settled = env3.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    env3.runtime.wait_report_containing("returned no result", Duration::from_secs(10)).await;
}

// DSH chain.spec: "skips a hook whose own time runs past the budget".
#[tokio::test(flavor = "multi_thread")]
async fn timeout_skips_a_busy_hook() {
    needs_node!();
    let env = Env::new("timeout").await;
    env.scenario("slow", json!({ "hooks": [{ "event": "tool.call", "behavior": "sleep_next", "sleepMs": 900 }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "slow": env.spec("slow", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, json!({ "hookMs": 80 })).await;

    let start = Instant::now();
    let settled = env.runtime.raise("tool.call", Some("s1"), json!({ "tool": "Bash" })).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    // The chain skipped the slow hook at ~80 ms instead of waiting 900 ms.
    assert!(start.elapsed() < Duration::from_secs(3), "chain took {:?}", start.elapsed());
    let line = env.runtime.wait_report_containing("ran past its 80 ms limit", Duration::from_secs(10)).await;
    assert!(line.contains("slow: tool.call hook skipped"), "unexpected report: {line}");
}

// Design §15.1: `prompt.edit` hooks get the 50 ms budget; sleep counts as
// the hook's own time.
#[tokio::test(flavor = "multi_thread")]
async fn prompt_edit_budget_is_short_and_sleep_counts_as_busy() {
    needs_node!();
    let env = Env::new("prompt-edit").await;
    env.scenario("slow", json!({ "hooks": [{ "event": "prompt.edit", "behavior": "sleep_next", "sleepMs": 900 }] }));
    let items = json!({ "slow": env.spec("slow", json!({})) });
    let _ready = env
        .hello_with(items, json!({ "hookMs": 60_000, "promptEditMs": 60 }))
        .await;

    let start = Instant::now();
    let settled = env.runtime.raise("prompt.edit", Some("s1"), json!({ "text": "hi" })).await;
    // The hook timed out; the event settles with the original input.
    assert_eq!(settled, json!({ "text": "hi" }));
    assert!(start.elapsed() < Duration::from_secs(3), "prompt.edit took {:?}", start.elapsed());
    env.runtime.wait_report_containing("ran past its 60 ms limit", Duration::from_secs(10)).await;
}

// DSH chain.spec: .catch answers in the failed hook's place.
#[tokio::test(flavor = "multi_thread")]
async fn catch_handler_answers_in_place() {
    needs_node!();
    let env = Env::new("catch").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "throw", "message": "boom", "catch": "answer", "catchAnswer": { "deny": "failed closed" } }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "deny": "failed closed" }));
}

// DSH chain.spec: the handler shares the hook's beneath state — nothing
// beneath runs twice.
#[tokio::test(flavor = "multi_thread")]
async fn catch_handler_shares_the_beneath_run() {
    needs_node!();
    let env = Env::new("catch-beneath").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "next_then_throw", "message": "after", "catch": "next", "catchAnswer": { "patched": true } }] }),
    );
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    // The handler got the already-resolved result from beneath and patched it.
    assert_eq!(settled, json!({ "patched": true }));
    assert_eq!(env.trace_count("b", "b:tool.call:begin"), 1, "beneath must run exactly once");
}

// Design §15.5 (P0 rule, still true in P1): a tool.call reroute — swapping
// the tool itself — is refused; the chain continues with the original input.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_reroutes_are_refused() {
    needs_node!();
    let env = Env::new("reroute").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "next", "ePatch": { "tool": "Edit" } }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({ "tool": "bash", "args": { "command": "ls" } })).await;
    assert_eq!(
        settled,
        json!({ "tool": "bash", "args": { "command": "ls" } }),
        "the logged call runs as logged (host spelling)"
    );
    let line = env.runtime.wait_report_containing("rerouted the call from Bash to Edit", Duration::from_secs(10)).await;
    assert!(line.contains("a: tool.call hook skipped"), "unexpected report: {line}");
}

// Design §15.5 (P1): an args rewrite now passes through — the rewritten args
// run beneath unchanged, and the event settles with them so the engine
// re-walks the permission policy.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_args_rewrites_pass_through() {
    needs_node!();
    let env = Env::new("args-rewrite").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "next", "ePatch": { "args": { "command": "ls -la" } } }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({ "tool": "bash", "args": { "command": "ls" } })).await;
    assert_eq!(settled, json!({ "tool": "bash", "args": { "command": "ls -la" } }));
    assert!(env.trace("a").contains("a:tool.call:begin"));
    // No refusal report: the rewrite is served, not skipped.
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(500) {
        assert!(
            env.runtime.take(|frame| frame.get("method").and_then(Value::as_str) == Some("mod.report")).is_none(),
            "an args rewrite must not report a skip"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

// Design §15.5 (P1): a `{result}` answer takes the tool.call event over — the
// tool does not execute and the object rides back to the runtime as-is.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_result_takeover_reaches_the_runtime() {
    needs_node!();
    let env = Env::new("takeover").await;
    env.scenario(
        "a",
        json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": { "value": "blocked by policy", "isError": true } } }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({ "tool": "bash", "args": {} })).await;
    assert_eq!(settled, json!({ "result": { "value": "blocked by policy", "isError": true } }));
}

// Design §15.5 (P1): an invalid `{result}` shape is a chain error — the hook
// skips with a report and the chain continues from its input; a non-object
// `{args}` rewrite fails the same way.
#[tokio::test(flavor = "multi_thread")]
async fn tool_call_invalid_answer_shapes_skip_the_hook() {
    needs_node!();
    let env = Env::new("bad-shapes").await;
    env.scenario("badresult", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": "text, not an object" } }] }));
    env.scenario("nullvalue", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": { "value": null } } }] }));
    env.scenario("badiserror", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "result": { "value": "x", "isError": "yes" } } }] }));
    env.scenario("badargs", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "args": "not an object" } }] }));
    let items = json!({
        "badresult": env.spec("badresult", json!({})),
        "nullvalue": env.spec("nullvalue", json!({})),
        "badiserror": env.spec("badiserror", json!({})),
        "badargs": env.spec("badargs", json!({})),
    });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({ "tool": "bash", "args": { "command": "ls" } })).await;
    // Every hook skipped: the event settles with the original input.
    assert_eq!(settled, json!({ "tool": "bash", "args": { "command": "ls" } }));
    env.runtime.wait_report_containing("badresult: tool.call hook skipped: answered with an invalid result takeover", Duration::from_secs(10)).await;
    env.runtime.wait_report_containing("nullvalue: tool.call hook skipped: answered with an invalid result takeover", Duration::from_secs(10)).await;
    env.runtime.wait_report_containing("badiserror: tool.call hook skipped: answered with an invalid result takeover", Duration::from_secs(10)).await;
    env.runtime.wait_report_containing("badargs: tool.call hook skipped: answered with an invalid args rewrite", Duration::from_secs(10)).await;
}

// Design §5.3: tool.check passes its `{decision}` three-value answers through
// to the runtime; a decision outside the three values is a chain error.
#[tokio::test(flavor = "multi_thread")]
async fn tool_check_decisions_pass_through() {
    needs_node!();
    let env = Env::new("tool-check").await;
    env.scenario("gate", json!({ "hooks": [{ "event": "tool.check", "behavior": "answer", "answer": { "decision": "ask", "reason": "confirm?" } }] }));
    let items = json!({ "gate": env.spec("gate", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.check", Some("s1"), json!({ "tool": "bash" })).await;
    assert_eq!(settled, json!({ "decision": "ask", "reason": "confirm?" }));

    let env2 = Env::new("tool-check-bad").await;
    env2.scenario("gate", json!({ "hooks": [{ "event": "tool.check", "behavior": "answer", "answer": { "decision": "maybe" } }] }));
    let items = json!({ "gate": env2.spec("gate", json!({})) });
    let _ready = env2.hello_with(items, default_budgets()).await;
    let settled = env2.runtime.raise("tool.check", Some("s1"), json!({ "tool": "bash" })).await;
    assert_eq!(settled, json!({ "tool": "bash" }), "an invalid decision skips the hook");
    let line = env2.runtime.wait_report_containing("answered with an invalid decision", Duration::from_secs(10)).await;
    assert!(line.contains("gate: tool.check hook skipped"), "unexpected report: {line}");
}

// Design §15.8: the broker maps Claude Code tool names both ways.
#[tokio::test(flavor = "multi_thread")]
async fn tool_name_aliases_apply_on_tool_call() {
    needs_node!();
    let env = Env::new("aliases").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "matcher": { "tool": "Read" }, "behavior": "answer", "answer": { "seen": "Read" } }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // Ginno's name reaches the chain as the mod-visible alias.
    let settled = env.runtime.raise("tool.call", Some("s1"), json!({ "tool": "read_file", "args": {} })).await;
    assert_eq!(settled, json!({ "seen": "Read" }));
}

// Design §3.2: a `$` call is an event — mods loaded before the caller see it.
#[tokio::test(flavor = "multi_thread")]
async fn op_calls_run_through_earlier_mods() {
    needs_node!();
    let env = Env::new("op-chain").await;
    env.scenario("early", json!({ "hooks": [{ "event": "state.get", "behavior": "next" }] }));
    env.scenario(
        "late",
        json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["state", "get", { "plugin": "early", "key": "k" }] }] }),
    );
    let items = json!({ "early": env.spec("early", json!({})), "late": env.spec("late", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("turn.complete", Some("s1"), json!({ "turnId": 1 })).await;
    assert_eq!(settled["ok"], true, "the call succeeded: {settled}");
    // DSH opCore shape: `{ value: null }` for a missing slot (the runner's
    // shim turns the inner null into undefined for the mod).
    assert_eq!(settled["value"], json!({ "value": null }), "the state slot is empty");
    env.wait_trace("early", "early:state.get:begin").await;

    // `tool.call` as an op never re-enters the chain (§15.2).
    let env2 = Env::new("op-toolcall").await;
    env2.scenario("watcher", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "denied": true } }] }));
    env2.scenario(
        "caller",
        json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["tool", "call", { "tool": "read_file", "path": "x" }] }] }),
    );
    let items = json!({ "watcher": env2.spec("watcher", json!({})), "caller": env2.spec("caller", json!({})) });
    let _ready = env2.hello_with(items, default_budgets()).await;
    // The op is forwarded to the runtime (which refuses here) — but crucially
    // the watcher mod was NOT consulted: tool.call never re-enters the chain.
    let settled = env2.runtime.raise("turn.complete", Some("s1"), json!({})).await;
    assert_eq!(settled["ok"], false, "{settled}");
    assert!(env2.trace("watcher").is_empty(), "tool.call op must not re-enter the chain");
}

#[tokio::test(flavor = "multi_thread")]
async fn store_roundtrip_and_machine_wide_cap() {
    needs_node!();
    let env = Env::new("store").await;
    env.scenario(
        "m",
        json!({ "hooks": [
            { "event": "turn.complete", "behavior": "single_call", "call": ["store", "set", { "key": "x", "value": 42 }] },
            { "event": "session.start", "behavior": "single_call", "call": ["store", "get", { "key": "x" }] },
            { "event": "tool.call", "behavior": "store_limit" },
        ] }),
    );
    let items = json!({ "m": env.spec("m", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("turn.complete", Some("s1"), json!({})).await;
    assert_eq!(settled["ok"], true, "{settled}");
    let settled = env.runtime.raise("session.start", Some("s1"), json!({})).await;
    assert_eq!(settled["value"], json!(42), "{settled}");

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled["ok"], false, "{settled}");
    assert_eq!(settled["code"], "error", "{settled}");
}

#[tokio::test(flavor = "multi_thread")]
async fn fs_is_root_limited_to_the_workspace() {
    needs_node!();
    let env = Env::new("fs").await;
    env.write_workspace("notes.txt", "hello");
    env.scenario(
        "m",
        json!({ "hooks": [
            { "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["fs", "read", { "path": "notes.txt" }],
                ["fs", "read", { "path": "../../etc/passwd" }],
                ["fs", "read", { "path": "/etc/passwd" }],
                ["fs", "write", { "path": "note.txt", "text": "written" }],
            ] },
        ] }),
    );
    let items = json!({ "m": env.spec("m", json!({ "fs.write": true })) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("turn.complete", Some("s1"), json!({})).await;
    let results = settled.as_array().expect("collect results");
    assert_eq!(results[0]["value"], json!("hello"));
    assert_eq!(results[1]["ok"], false);
    assert!(results[1]["message"].as_str().unwrap().contains("outside the workspace"));
    assert_eq!(results[2]["ok"], false);
    assert!(results[2]["message"].as_str().unwrap().contains("outside the workspace"));
    assert_eq!(results[3]["ok"], true);
    assert_eq!(std::fs::read_to_string(env.dir.join("workspace/note.txt")).unwrap(), "written");
}

#[tokio::test(flavor = "multi_thread")]
async fn grants_deny_unauthorized_calls() {
    needs_node!();
    let env = Env::new("grants").await;
    env.scenario(
        "gated",
        json!({ "hooks": [
            { "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["process", "run", { "argv": ["git", "status"] }],
                ["env", "get", { "name": "AWS_SECRET" }],
                ["http", "fetch", { "url": "https://example.com" }],
                ["env", "get", { "name": "PATH" }],
            ] },
        ] }),
    );
    let items = json!({ "gated": env.spec("gated", json!({ "env.get": ["PATH"] })) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("turn.complete", Some("s1"), json!({})).await;
    let results = settled.as_array().expect("collect results");
    assert_eq!(results[0]["code"], "denied", "process.run without a grant");
    assert_eq!(results[1]["code"], "denied", "env.get outside the grant");
    assert_eq!(results[2]["code"], "denied", "http.fetch without a grant");
    assert_eq!(results[3]["ok"], true, "PATH is in the env grant: {}", results[3]);
    // Every denial points at the fix: grants on the Mods settings page (§8).
    assert!(
        results[0]["message"].as_str().unwrap().contains("Mods settings page"),
        "{}",
        results[0]["message"]
    );
    assert!(
        results[2]["message"].as_str().unwrap().contains("Mods settings page"),
        "{}",
        results[2]["message"]
    );
}

// Design §3.4: renders subscribe to the state slots they read; a write
// redraws the band (one generation per tree change).
#[tokio::test(flavor = "multi_thread")]
async fn state_writes_redraw_subscribed_bands() {
    needs_node!();
    let env = Env::new("subscribe").await;
    env.scenario("a", json!({ "hooks": [{ "event": "ui.render", "behavior": "render_state", "stateRef": { "plugin": "a", "key": "v" } }] }));
    env.scenario(
        "b",
        json!({ "hooks": [{ "event": "turn.complete", "behavior": "calls_collect", "calls": [
            ["state", "set", { "plugin": "a", "key": "v", "value": 2 }],
            ["state", "set", { "plugin": "b", "key": "unrelated", "value": 9 }],
        ] }] }),
    );
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // session.start triggers the first drawing.
    let _ = env.runtime.raise("session.start", Some("s1"), json!({})).await;
    let first = env.runtime.wait_notify("bands.update", Duration::from_secs(30)).await;
    assert_eq!(first["args"]["generation"], 1);
    assert!(first["args"]["tree"].to_string().contains("count null"), "got {}", first);

    // One turn writes a subscribed slot AND an unrelated one: exactly one
    // redraw, with the new value (burst coalesced, unrelated write ignored).
    let _ = env.runtime.raise("turn.complete", Some("s1"), json!({})).await;
    let second = env.runtime.wait_notify("bands.update", Duration::from_secs(30)).await;
    assert_eq!(second["args"]["generation"], 2);
    assert!(second["args"]["tree"].to_string().contains("count 2"), "got {}", second);
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(2) {
        assert!(
            env.runtime.take(|frame| frame.get("method").and_then(Value::as_str) == Some("bands.update")).is_none(),
            "an unrelated state write must not redraw"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn press_routes_to_the_runner_and_stale_generations_are_ignored() {
    needs_node!();
    let env = Env::new("press").await;
    env.scenario("a", json!({ "hooks": [{ "event": "ui.render", "behavior": "render_button" }] }));
    let items = json!({ "a": env.spec("a", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let _ = env.runtime.raise("session.start", Some("s1"), json!({})).await;
    let drawn = env.runtime.wait_notify("bands.update", Duration::from_secs(30)).await;
    assert_eq!(drawn["args"]["generation"], 1);
    assert!(drawn["args"]["tree"].to_string().contains("a:a0"), "got {}", drawn);

    // A current-generation press reaches the runner.
    env.runtime.notify("ui.press", json!({ "generation": 1, "actionId": "a:a0" }), Some("s1"));
    env.wait_trace("a", "a:press:a:a0").await;

    // A pane Input/Select submission rides the press frame as `value`; the
    // runner call carries it through (observable in the mock's trace).
    env.runtime
        .notify("ui.press", json!({ "generation": 1, "actionId": "a:a0", "value": "cm" }), Some("s1"));
    env.wait_trace("a", r#"a:press:a:a0:"cm""#).await;

    // A stale-generation press is ignored with a report.
    env.runtime.notify("ui.press", json!({ "generation": 0, "actionId": "a:a0" }), Some("s1"));
    let line = env.runtime.wait_report_containing("band press ignored", Duration::from_secs(10)).await;
    assert!(line.contains("generation 0"), "unexpected report: {line}");
}

// Design §7.1 per-mod bands: each mod with a `ui.render` hook draws its own
// segment — one mod's tree never blanks another's, presses validate against
// the owning mod's generation, and the payload keeps the first non-empty
// tree as the legacy `tree` field.
#[tokio::test(flavor = "multi_thread")]
async fn each_mod_draws_its_own_band_segment() {
    needs_node!();
    let env = Env::new("per-mod").await;
    env.scenario("a", json!({ "hooks": [{ "event": "ui.render", "behavior": "render_button" }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "ui.render", "behavior": "render_button" }] }));
    let items = json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let _ = env.runtime.raise("session.start", Some("s1"), json!({})).await;
    let drawn = env.runtime.wait_notify("bands.update", Duration::from_secs(30)).await;
    let mods = drawn["args"]["mods"].as_array().expect("a per-mod mods array");
    assert_eq!(mods.len(), 2, "both mods draw: {drawn}");
    assert_eq!(mods[0]["mod"], "a");
    assert_eq!(mods[1]["mod"], "b");
    assert_eq!(mods[0]["generation"], 1);
    assert_eq!(mods[1]["generation"], 1);
    assert!(mods[0]["tree"].to_string().contains("a:a0"), "got {drawn}");
    assert!(mods[1]["tree"].to_string().contains("b:a0"), "got {drawn}");
    // Legacy fields mirror the first non-empty mod (a, in load order).
    assert_eq!(drawn["args"]["generation"], 1);
    assert!(drawn["args"]["tree"].to_string().contains("a:a0"), "got {drawn}");

    // A press names its mod by the action id prefix; b's own generation 1 is
    // valid even though a's tree came first.
    env.runtime.notify("ui.press", json!({ "generation": 1, "actionId": "b:a0" }), Some("s1"));
    env.wait_trace("b", "b:press:b:a0").await;

    // A generation from the other mod's drawing does not validate for a.
    env.runtime.notify("ui.press", json!({ "generation": 99, "actionId": "a:a0" }), Some("s1"));
    let line = env.runtime.wait_report_containing("band press ignored", Duration::from_secs(10)).await;
    assert!(line.contains("a:a0"), "unexpected report: {line}");
}

// Design §7.4 panes, Rust side: `$.ui.open` records the pane and reports
// `isPlaced`, the next render pass draws it through the owner's chain and
// pushes `mod.pane.update`, `$.ui.close`/`session.end` push `mod.pane.close`
// and clear it, and the 4-panes-per-session cap holds.
#[tokio::test(flavor = "multi_thread")]
async fn panes_open_render_close_and_clear_on_session_end() {
    needs_node!();
    let env = Env::new("panes").await;
    env.scenario(
        "p",
        json!({ "hooks": [
            { "event": "ui.render", "behavior": "render_button" },
            { "event": "turn.complete", "behavior": "calls_collect", "calls": [
                ["ui", "open", { "id": "w1", "title": "Weather" }],
                ["ui", "open", { "id": "w2", "title": "Two" }],
                ["ui", "open", { "id": "w3", "title": "Three" }],
                ["ui", "open", { "id": "w4", "title": "Four" }],
                ["ui", "open", { "id": "w5", "title": "Five" }],
            ] },
            { "event": "session.end", "behavior": "calls_collect", "calls": [
                ["ui", "close", { "id": "w1" }],
            ] },
        ] }),
    );
    let items = json!({ "p": env.spec("p", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // Four panes open; the fifth is refused by the cap. Each open reports
    // isPlaced and reaches Python as `mod.pane.open`.
    let settled = env.runtime.raise("turn.complete", Some("s1"), json!({ "turnId": 1 })).await;
    let results = settled.as_array().expect("collect results");
    assert_eq!(results[0]["value"]["isPlaced"], true, "{settled}");
    assert_eq!(results[0]["value"]["id"], "w1");
    assert_eq!(results[4]["ok"], false, "the fifth pane is over the cap: {settled}");
    assert!(results[4]["message"].as_str().unwrap().contains("at most 4 panes"));
    for id in ["w1", "w2", "w3", "w4"] {
        let opened = env.runtime.wait_notify("mod.pane.open", Duration::from_secs(30)).await;
        assert_eq!(opened["args"]["id"], id, "got {opened}");
        assert_eq!(opened["mod_name"], "p");
    }

    // The render pass draws each pane through the owner's ui.render chain.
    for id in ["w1", "w2", "w3", "w4"] {
        let updated = env.runtime.wait_notify("mod.pane.update", Duration::from_secs(30)).await;
        assert_eq!(updated["args"]["id"], id, "got {updated}");
        assert_eq!(updated["args"]["generation"], 1);
        assert!(updated["args"]["tree"].to_string().contains("p:a0"), "got {updated}");
        assert_eq!(updated["mod_name"], "p");
    }

    // ui.panes lists them, oldest first.
    // (Checked from the broker side below via session.end's close order.)
    let _ = env
        .runtime
        .raise("session.end", Some("s1"), json!({}))
        .await;
    // session.end first runs the mod's own close of w1, then clears the rest.
    // The session.end chain settles, then forget_session closes every pane.
    let mut closed = Vec::new();
    for _ in 0..4 {
        let frame = env.runtime.wait_notify("mod.pane.close", Duration::from_secs(30)).await;
        closed.push(frame["args"]["id"].as_str().unwrap().to_string());
    }
    closed.sort();
    assert_eq!(closed, vec!["w1", "w2", "w3", "w4"], "every open pane closes");

    // The band table is gone too: a further draw would re-open, so a fresh
    // session's pane cap starts clean.
    let panes = env.broker.surfaces_panes("s1");
    assert!(panes.is_empty(), "session.end cleared the pane table");
}

// Design §15.2 v2 addition: a runner crash fails its own hook; the chain
// continues.
#[tokio::test(flavor = "multi_thread")]
async fn runner_crash_fails_its_hook_and_the_chain_continues() {    needs_node!();
    let env = Env::new("crash").await;
    env.scenario("doomed", json!({ "hooks": [{ "event": "tool.call", "behavior": "crash" }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "ran": "b" } }] }));
    let items = json!({ "doomed": env.spec("doomed", json!({})), "b": env.spec("b", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "ran": "b" }));
    let line = env.runtime.wait_report_containing("hook skipped", Duration::from_secs(10)).await;
    assert!(line.contains("doomed"), "unexpected report: {line}");
}

#[tokio::test(flavor = "multi_thread")]
async fn config_reload_unloads_disabled_mods() {
    needs_node!();
    let env = Env::new("reload").await;
    env.scenario("a", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "from": "a" } }] }));
    env.scenario("b", json!({ "hooks": [{ "event": "tool.call", "behavior": "answer", "answer": { "from": "b" } }] }));
    let _ready = env
        .hello_with(json!({ "a": env.spec("a", json!({})), "b": env.spec("b", json!({})) }), default_budgets())
        .await;

    // b goes away: its hooks stop, a's keep working.
    let ready = env
        .runtime
        .apply_config(&env.config(json!({ "a": env.spec("a", json!({})) }), default_budgets()))
        .await;
    assert_eq!(ready["mods"].as_array().unwrap().len(), 1);
    assert_eq!(ready["mods"][0]["name"], "a");
    assert_eq!(ready["mods"][0]["status"], "loaded");
    let settled = env.runtime.raise("tool.call", Some("s1"), json!({})).await;
    assert_eq!(settled, json!({ "from": "a" }));
    let status = env.broker.status_payload().await;
    let mods = status["mods"].as_array().unwrap();
    assert_eq!(mods.len(), 1, "b should be gone: {status}");
    assert_eq!(mods[0]["name"], "a");
}

// ---------------------------------------------------------------------------
// $.ui.ask (design §7.3): mod → broker pending → runtime mod.ask → broker.answer
// ---------------------------------------------------------------------------

// The pending-ask table is process-global and the disconnect test's
// `fail_all_asks` sweeps every ask in it, so the two tests serialize.
static ASK_TESTS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[tokio::test(flavor = "multi_thread")]
async fn ui_ask_round_trips_through_the_runtime() {
    needs_node!();
    let _guard = ASK_TESTS.lock().await;
    let env = Env::new("ui-ask").await;
    env.scenario("asking", json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["ui", "ask", { "message": "Proceed?", "choices": ["yes", "no"] }] }] }));
    let items = json!({ "asking": env.spec("asking", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    // The event only settles once the test (as the runtime) answers.
    let event_id = env.runtime.send_event("turn.complete", Some("s1"), json!({ "turnId": 1 }));
    let ask = env.runtime.wait_notify("mod.ask", Duration::from_secs(30)).await;
    assert_eq!(ask["args"]["message"], "Proceed?");
    assert_eq!(ask["args"]["choices"], json!(["yes", "no"]));
    assert_eq!(ask["mod_name"], "asking");
    assert_eq!(ask["session"], "s1");
    let ask_id = ask["args"]["id"].as_str().expect("an ask id").to_string();

    // The runtime's answer resolves the pending ask; the runner's `$` call
    // settles with the op's `{value}` shape.
    let answer_id = env.runtime.call_broker("answer", json!({ "id": ask_id, "value": "yes" }));
    let ack = env.runtime.wait_result(answer_id, "broker.answer", Duration::from_secs(10)).await.expect("answer ack");
    assert_eq!(ack, json!({ "resolved": true }));

    let settled = env.runtime.wait_result(event_id, "turn.complete", Duration::from_secs(30)).await.expect("event settles");
    assert_eq!(settled, json!({ "ok": true, "value": { "value": "yes" } }));
    env.wait_trace("asking", r#"asking:call:ui.ask:{"ok":true,"value":{"value":"yes"}}"#).await;

    // Answering an id that names no ask is a not-found, not a silent ok.
    let bogus = env.runtime.call_broker("answer", json!({ "id": "ask-gone", "value": "x" }));
    let err = env.runtime.wait_result(bogus, "bogus answer", Duration::from_secs(10)).await.unwrap_err();
    assert!(err.contains("no pending ask"), "{err}");
}

#[tokio::test(flavor = "multi_thread")]
async fn ui_ask_fails_when_the_runtime_disconnects() {
    needs_node!();
    let _guard = ASK_TESTS.lock().await;
    let env = Env::new("ui-ask-disconnect").await;
    env.scenario("asking", json!({ "hooks": [{ "event": "turn.complete", "behavior": "single_call", "call": ["ui", "ask", { "message": "Still there?" }] }] }));
    let items = json!({ "asking": env.spec("asking", json!({})) });
    let _ready = env.hello_with(items, default_budgets()).await;

    let _event_id = env.runtime.send_event("turn.complete", Some("s1"), json!({}));
    let ask = env.runtime.wait_notify("mod.ask", Duration::from_secs(30)).await;
    assert_eq!(ask["args"]["choices"], Value::Null, "no choices field when the mod sent none");

    // The runtime goes away mid-question: the ask aborts, the runner's call
    // errors, and its trace keeps the proof (the event result itself has no
    // runtime left to reach).
    env.runtime.disconnect().await;
    env.wait_trace("asking", r#"asking:call:ui.ask:{"ok":false"#).await;
    let trace = env.trace("asking");
    assert!(trace.contains("runtime disconnected"), "{trace}");
}

// ---------------------------------------------------------------------------
// the real runner: the vendored Token Weather example over the actual
// packages/mod-runner source (dev mode: node type stripping)
// ---------------------------------------------------------------------------

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_path_buf()
}

#[tokio::test(flavor = "multi_thread")]
async fn real_runner_draws_the_token_weather_band() {
    needs_node!();
    let runner_src = repo_root().join("packages/mod-runner/src/index.ts");
    let mod_dir = repo_root().join("packages/mod-runner/examples/token-weather");
    if !runner_src.exists() || !mod_dir.exists() {
        eprintln!("skipping: packages/mod-runner not present");
        return;
    }

    let env = Env::new("real-token-weather").await;
    // The runner asks for the pushed config before registering; the spec is
    // what `runner.loaded` answers with.
    let spec = json!({
        "enabled": true,
        "dir": mod_dir,
        "grants": {},
        "config": {},
    });
    let config = json!({
        "nodePath": node_bin(),
        "runnerPath": runner_src,
        "workspace": env.dir.join("workspace"),
        "storePath": env.dir.join("store.json"),
        "logsDir": env.dir.join("logs"),
        "mods": { "items": { "token-weather": spec } },
        "budgets": {},
    });

    // Answer forwarded ops like the runtime would: session.usage feeds the
    // mod's readings; anything else is refused.
    let session_usage = json!({ "context": { "tokens": 32_000, "window": 200_000, "percent": 16 } });
    env.runtime.push_responder(move |ns, method| {
        if ns == "session" && method == "usage" {
            Some(session_usage.clone())
        } else {
            None
        }
    });

    let ready = env.runtime.hello(&config).await;
    let first = &ready["mods"][0];
    assert_eq!(first["name"], "token-weather", "{ready}");
    assert_eq!(first["status"], "loaded", "{ready}");
    assert!(first["hooks"].as_str().unwrap().contains("session.start"), "{ready}");

    // session.start: the hook takes a reading through the runtime, then the
    // band draws with one data point.
    let _ = env.runtime.raise("session.start", Some("s1"), json!({})).await;
    let drawn = env.runtime.wait_notify("bands.update", Duration::from_secs(30)).await;
    assert_eq!(drawn["args"]["generation"], 1);
    assert!(
        drawn["args"]["tree"].to_string().contains("of context"),
        "band should show the forecast: {}",
        drawn
    );

    // turn.complete: a second reading lands, the band redraws with a trend.
    let _ = env.runtime.raise("turn.complete", Some("s1"), json!({ "turnId": 1 })).await;
    let redrawn = env.runtime.wait_notify("bands.update", Duration::from_secs(30)).await;
    assert_eq!(redrawn["args"]["generation"], 2);
    assert!(
        redrawn["args"]["tree"].to_string().contains("last turns"),
        "band with two readings shows the trend: {}",
        redrawn
    );
}
