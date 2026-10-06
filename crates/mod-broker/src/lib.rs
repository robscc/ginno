//! ginno-mod-broker: the center of the Ginno Claude Code Mods compatibility
//! bus (design §3). One crate, two hosts: linked into the Tauri desktop app
//! and run standalone as the `ginno-mod-broker` CLI in dev/web mode.
//!
//! Responsibilities:
//! - chain orchestration — dispatch, distributed `next()` single-run
//!   semantics, BudgetClock busy-time accounting, `.catch`, matcher pre-filter
//!   ([`chain`], [`budget`], [`matcher`])
//! - the op table — broker-local implementations of state/store/clock/fs/
//!   http/process/env/ui plus grants enforcement, and forwards of runtime-
//!   backed ops to Python ([`ops`], [`grants`])
//! - SurfaceTable — per-session band rendering, generations, action press,
//!   subscription redraws ([`surfaces`])
//! - runner supervision — one Node process per mod, handshake, heartbeat,
//!   crash backoff ([`runner`])
//! - the Python channel — Unix socket + token, config push, engine events,
//!   op forwards ([`runtime`])
//!
//! The broker holds no on-disk config: Python owns settings.json and pushes
//! everything; the broker's own state (`$.store`) lives at the store path the
//! config names.

pub mod aliases;
pub mod budget;
pub mod chain;
pub mod grants;
pub mod matcher;
pub mod ops;
pub mod protocol;
pub mod registry;
pub mod runner;
pub mod runtime;
pub mod store;
pub mod surfaces;
pub mod timers;

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot};

use aliases::ToolNameAliases;
use budget::Budget;
use chain::{ChainCore, ChainCtx, Hub};
use protocol::{Frame, RpcError, CODE_ERROR, PROTOCOL_VERSION};
use registry::{ModEntry, Registry};
use runtime::RuntimeConn;
use store::Store;
use timers::TimerSet;

pub use protocol::{result_err, result_ok, Frame as WireFrame, HookFailure};

/// One configured mod as Python pushed it.
#[derive(Debug, Clone, PartialEq)]
pub struct ModSpec {
    pub name: String,
    pub dir: String,
    /// Raw `mods.<name>.grants` JSON; parsed per call by [`grants::Grants`].
    pub grants: Value,
    /// Raw `mods.<name>.config` JSON (the mod's userConfig).
    pub config: Value,
}

/// Everything the broker needs that Python pushed (design §9: settings.json
/// stays Python's; this is a projection).
#[derive(Debug, Clone)]
pub struct Config {
    pub node_path: String,
    pub runner_path: String,
    pub workspace: PathBuf,
    pub logs_dir: PathBuf,
    pub store_path: Option<PathBuf>,
    /// A hook's own running-time limit (10 s).
    pub hook_ms: u64,
    /// `prompt.edit` hooks get Claude Code's 50 ms.
    pub prompt_edit_ms: u64,
    /// A `.catch` handler's limit (1 s).
    pub catch_ms: u64,
    /// The whole `session.end` chain's shared limit (1.5 s).
    pub session_end_ms: u64,
    pub process_timeout_ms: u64,
    pub process_max_timeout_ms: u64,
    pub band_columns: u32,
    pub band_rows: u32,
    /// Enabled mods in load order (config map order).
    pub mods: Vec<ModSpec>,
}

impl Config {
    /// The per-hook budget for one event name.
    pub fn budget_for(&self, event: &str) -> u64 {
        if event == "prompt.edit" {
            self.prompt_edit_ms
        } else {
            self.hook_ms
        }
    }
}

fn get_u64(value: &Value, field: &str, default: u64) -> u64 {
    value.get(field).and_then(Value::as_u64).unwrap_or(default)
}

pub fn parse_config(value: &Value) -> Result<Config, String> {
    let node_path = value
        .get("nodePath")
        .and_then(Value::as_str)
        .ok_or("config needs nodePath (the discovered node binary)")?
        .to_string();
    let runner_path = value
        .get("runnerPath")
        .and_then(Value::as_str)
        .ok_or("config needs runnerPath (the mod-runner.mjs bundle)")?
        .to_string();
    let workspace = value
        .get("workspace")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    let logs_dir = value
        .get("logsDir")
        .and_then(Value::as_str)
        .map(|p| PathBuf::from(p.replace("~/", &format!("{}/", home_dir()))))
        .unwrap_or_else(default_logs_dir);
    let budgets = value.get("budgets").cloned().unwrap_or(Value::Null);
    let band = value.get("band").cloned().unwrap_or(Value::Null);
    let mut mods = Vec::new();
    let items = value.pointer("/mods/items").and_then(Value::as_object);
    if let Some(items) = items {
        // preserve_order keeps the config's mod order, which is the load order.
        for (name, item) in items {
            if item.get("enabled").and_then(Value::as_bool) == Some(false) {
                continue;
            }
            let dir = item
                .get("dir")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("mods.items.{name} needs a dir"))?
                .to_string();
            mods.push(ModSpec {
                name: name.clone(),
                dir,
                grants: item.get("grants").cloned().unwrap_or(Value::Null),
                config: item.get("config").cloned().unwrap_or(Value::Null),
            });
        }
    }
    Ok(Config {
        node_path,
        runner_path,
        workspace,
        logs_dir,
        store_path: value.get("storePath").and_then(Value::as_str).map(PathBuf::from),
        hook_ms: get_u64(&budgets, "hookMs", 10_000),
        prompt_edit_ms: get_u64(&budgets, "promptEditMs", 50),
        catch_ms: get_u64(&budgets, "catchMs", 1_000),
        session_end_ms: get_u64(&budgets, "sessionEndMs", 1_500),
        process_timeout_ms: get_u64(&budgets, "processTimeoutMs", 30_000),
        process_max_timeout_ms: get_u64(&budgets, "processMaxTimeoutMs", 600_000),
        band_columns: get_u64(&band, "columns", 80) as u32,
        band_rows: get_u64(&band, "rows", 8) as u32,
        mods,
    })
}

fn home_dir() -> String {
    std::env::var("HOME").unwrap_or_else(|_| ".".to_string())
}

fn default_logs_dir() -> PathBuf {
    Path::new(&home_dir()).join(".ginno").join("logs")
}

/// One serialized unit of a session's event lane: engine events of one
/// session dispatch one at a time; other sessions run in parallel.
struct Job {
    task: std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>,
}

/// The broker core. Cheap to clone (all fields are Arc/Mutex); ownership is
/// per-process — the Tauri app or the CLI.
pub struct Broker {
    pub config: std::sync::RwLock<Config>,
    pub registry: std::sync::RwLock<Registry>,
    pub runners: tokio::sync::RwLock<HashMap<String, Arc<runner::RunnerConn>>>,
    pub runtime: std::sync::RwLock<Option<Arc<RuntimeConn>>>,
    pub hub: Hub,
    pub aliases: ToolNameAliases,
    pub store: Store,

    /// `$.state`: session key → `<plugin>\u{0}<key>` slot → value.
    session_state: Mutex<HashMap<String, HashMap<String, Value>>>,
    /// Timers by `<session>\u{0}<mod>`.
    timers: Mutex<HashMap<String, Arc<TimerSet>>>,
    /// Budgets by `<invocation>\u{0}<mod>`: the hook run `$` calls pause.
    pub budgets: Mutex<HashMap<(String, String), Arc<Budget>>>,
    /// Band table ([`surfaces`]).
    pub bands: Mutex<HashMap<String, Arc<surfaces::Band>>>,

    /// Desired mods from the last applied config.
    specs: Mutex<HashMap<String, ModSpec>>,
    grants: Mutex<HashMap<String, grants::Grants>>,
    /// Mods being torn down (their runner exits are not restarts).
    unloading: Mutex<HashSet<String>>,
    restarts: Mutex<HashMap<String, u32>>,
    runner_gens: Mutex<HashMap<String, u64>>,
    invocation_seq: AtomicU64,
    lanes: Mutex<HashMap<String, mpsc::UnboundedSender<Job>>>,
}

impl Broker {
    pub fn new(store_path: Option<PathBuf>) -> Arc<Broker> {
        Arc::new(Broker {
            config: std::sync::RwLock::new(empty_config()),
            registry: std::sync::RwLock::new(Registry::default()),
            runners: tokio::sync::RwLock::new(HashMap::new()),
            runtime: std::sync::RwLock::new(None),
            hub: Hub::default(),
            aliases: ToolNameAliases::new(),
            store: Store::new(store_path),
            session_state: Mutex::new(HashMap::new()),
            timers: Mutex::new(HashMap::new()),
            budgets: Mutex::new(HashMap::new()),
            bands: Mutex::new(HashMap::new()),
            specs: Mutex::new(HashMap::new()),
            grants: Mutex::new(HashMap::new()),
            unloading: Mutex::new(HashSet::new()),
            restarts: Mutex::new(HashMap::new()),
            runner_gens: Mutex::new(HashMap::new()),
            invocation_seq: AtomicU64::new(1),
            lanes: Mutex::new(HashMap::new()),
        })
    }

    // ---- config ----

    /// Apply a pushed config: diff against the previous one, unload removed
    /// or changed mods, load new ones, and report per-mod results (idempotent
    /// on identical configs — design §5.2).
    pub async fn apply_config(self: &Arc<Self>, value: &Value) -> Value {
        let config = match parse_config(value) {
            Ok(config) => config,
            Err(e) => return json!({ "error": e }),
        };
        let old_specs: HashMap<String, ModSpec> = std::mem::take(&mut *self.specs.lock().unwrap());
        let new_specs: HashMap<String, ModSpec> =
            config.mods.iter().map(|spec| (spec.name.clone(), spec.clone())).collect();

        *self.config.write().unwrap() = config.clone();
        *self.grants.lock().unwrap() = config
            .mods
            .iter()
            .map(|spec| (spec.name.clone(), grants::Grants::from_json(&spec.grants)))
            .collect();

        let mut to_load: Vec<ModSpec> = Vec::new();
        let mut to_unload: Vec<String> = Vec::new();
        for (name, old) in &old_specs {
            match new_specs.get(name) {
                None => to_unload.push(name.clone()),
                Some(new) if new != old => {
                    to_unload.push(name.clone());
                    to_load.push(new.clone());
                }
                Some(_) => {}
            }
        }
        for spec in &config.mods {
            if !old_specs.contains_key(&spec.name) {
                to_load.push(spec.clone());
            }
        }
        *self.specs.lock().unwrap() = new_specs;

        for name in &to_unload {
            self.unload_mod(name).await;
        }
        // Load in parallel; each waits for its runner's handshake.
        let loads: Vec<_> = to_load
            .iter()
            .map(|spec| {
                let broker = self.clone();
                let spec = spec.clone();
                tokio::spawn(async move {
                    let gen = broker.next_runner_gen(&spec.name);
                    runner::load(&broker, &spec.name, gen).await
                })
            })
            .collect();
        let mut load_results: HashMap<String, Result<(), String>> = HashMap::new();
        for (index, load) in loads.into_iter().enumerate() {
            let name = to_load[index].name.clone();
            let result = load.await.unwrap_or_else(|e| Err(format!("load task failed: {e}")));
            load_results.insert(name, result);
        }

        // The ready payload: every desired mod with its status.
        let registry = self.registry.read().unwrap();
        let mods: Vec<Value> = config
            .mods
            .iter()
            .map(|spec| {
                let loaded = registry.order_of(&spec.name).is_some();
                let mut entry = json!({
                    "name": spec.name,
                    "status": if loaded { "loaded" } else { "error" },
                });
                if loaded {
                    entry["hooks"] = json!(registry.describe(&spec.name));
                    let unserved = registry.unserved(&spec.name);
                    if !unserved.is_empty() {
                        entry["unserved"] = json!(unserved);
                    }
                }
                if let Some(Err(e)) = load_results.get(&spec.name) {
                    entry["error"] = json!(e);
                }
                entry
            })
            .collect();
        json!({ "mods": mods })
    }

    pub fn mod_spec(&self, name: &str) -> Option<ModSpec> {
        self.specs.lock().unwrap().get(name).cloned()
    }

    pub fn grants_for(&self, name: &str) -> grants::Grants {
        self.grants.lock().unwrap().get(name).cloned().unwrap_or_default()
    }

    pub fn next_runner_gen(&self, name: &str) -> u64 {
        let mut gens = self.runner_gens.lock().unwrap();
        let next = gens.entry(name.to_string()).or_insert(0);
        *next += 1;
        *next
    }

    pub fn bump_restart_count(&self, name: &str) -> u32 {
        let mut restarts = self.restarts.lock().unwrap();
        let count = restarts.entry(name.to_string()).or_insert(0);
        *count += 1;
        *count
    }

    pub fn mod_unloading(&self, name: &str) -> bool {
        self.unloading.lock().unwrap().contains(name)
    }

    /// Tear one mod down: out of the registry, its runner killed, its timers
    /// closed and drained.
    pub async fn unload_mod(&self, name: &str) {
        self.unloading.lock().unwrap().insert(name.to_string());
        self.registry.write().unwrap().remove(name);
        if let Some(conn) = self.runners.write().await.remove(name) {
            conn.kill().await;
        }
        let keys: Vec<String> = self
            .timers
            .lock()
            .unwrap()
            .keys()
            .filter(|key| key.ends_with(&format!("\u{0}{name}")))
            .cloned()
            .collect();
        for key in keys {
            let set = self.timers.lock().unwrap().remove(&key);
            if let Some(set) = set {
                set.close().await;
            }
        }
        self.unloading.lock().unwrap().remove(name);
    }

    /// Three strikes: stop restarting, drop the mod, tell Python.
    pub async fn disable_mod(&self, name: &str) {
        self.report(&format!("{name}: runner failed 3 restarts; disabling"));
        self.unload_mod(name).await;
        self.notify_mod_status(name, "disabled");
    }

    /// Register the hooks a runner reported at handshake; warns once per
    /// engine event this host never raises (design §15.8).
    pub async fn register_mod_hooks(self: &Arc<Self>, name: &str, args: &Value) -> Result<(), String> {
        let hooks = registry::hooks_from_json(name, args)?;
        let dir = self.mod_spec(name).map(|spec| spec.dir.clone()).unwrap_or_default();
        {
            let mut reg = self.registry.write().unwrap();
            reg.remove(name); // idempotent over a reload of the same mod
            let entry = ModEntry {
                name: name.to_string(),
                dir,
                order: 0,
                hooks_line: registry_hooks_line(&hooks),
            };
            reg.add(entry, hooks).map_err(|e| format!("{name}: {e}"))?;
            // Parallel loads handshake out of order; the config order is law.
            let names: Vec<String> = self.config.read().unwrap().mods.iter().map(|spec| spec.name.clone()).collect();
            reg.sort_by(&names);
        }
        self.restarts.lock().unwrap().remove(name);
        let reg = self.registry.read().unwrap();
        for event in reg.unserved(name) {
            self.report(&format!("{name}: registered, but this host never raises that event: {event}"));
        }
        Ok(())
    }

    pub fn notify_mod_status(&self, name: &str, status: &str) {
        self.runtime_notify("mod.status", None, Some(name.to_string()), json!({ "name": name, "status": status }));
    }

    /// The `broker.status` payload for the settings page.
    pub async fn status_payload(self: &Arc<Self>) -> Value {
        let registry = self.registry.read().unwrap();
        let specs = self.specs.lock().unwrap();
        let mods: Vec<Value> = specs
            .keys()
            .map(|name| {
                let loaded = registry.order_of(name).is_some();
                json!({
                    "name": name,
                    "status": if loaded { "loaded" } else { "disabled" },
                    "hooks": registry.describe(name),
                })
            })
            .collect();
        json!({ "mods": mods })
    }

    // ---- runners & state ----

    pub async fn runner(&self, name: &str) -> Option<Arc<runner::RunnerConn>> {
        self.runners.read().await.get(name).cloned()
    }

    pub fn state_get(&self, session: &str, slot: &str) -> Value {
        self.session_state
            .lock()
            .unwrap()
            .get(session)
            .and_then(|values| values.get(slot))
            .cloned()
            .unwrap_or(Value::Null)
    }

    pub fn state_set(&self, session: &str, slot: &str, value: Value) {
        self.session_state
            .lock()
            .unwrap()
            .entry(session.to_string())
            .or_default()
            .insert(slot.to_string(), value);
    }

    pub fn next_invocation(&self) -> String {
        format!("i-{}", self.invocation_seq.fetch_add(1, Ordering::SeqCst))
    }

    pub fn timers_of(self: &Arc<Self>, session: &Option<String>, mod_name: &str) -> Arc<TimerSet> {
        let key = format!("{}\u{0}{}", session.clone().unwrap_or_default(), mod_name);
        if let Some(set) = self.timers.lock().unwrap().get(&key) {
            return set.clone();
        }
        let set = TimerSet::new(Arc::downgrade(self), session.clone(), mod_name.to_string());
        self.timers.lock().unwrap().insert(key, set.clone());
        set
    }

    /// Resolve a `$.fs` path inside the workspace root; symlinks are followed
    /// by canonicalization, so a link pointing outside is caught.
    pub fn workspace_path(&self, path: &str) -> Result<PathBuf, RpcError> {
        let root = self.config.read().unwrap().workspace.clone();
        let root_canonical = root
            .canonicalize()
            .map_err(|e| RpcError::new(CODE_ERROR, format!("workspace root is not accessible: {e}")))?;
        let raw = if Path::new(path).is_absolute() { PathBuf::from(path) } else { root_canonical.join(path) };
        let target = match raw.canonicalize() {
            Ok(target) => target,
            Err(_) => {
                // Not there yet (a write target): anchor on its existing parent.
                let parent = raw
                    .parent()
                    .ok_or_else(|| RpcError::new(CODE_ERROR, format!("$.fs: invalid path {path}")))?;
                let parent_canonical = parent
                    .canonicalize()
                    .map_err(|_| RpcError::new(CODE_ERROR, format!("$.fs: {path} is outside the workspace")))?;
                match raw.file_name() {
                    Some(name) => parent_canonical.join(name),
                    None => parent_canonical,
                }
            }
        };
        if !target.starts_with(&root_canonical) {
            return Err(RpcError::new(CODE_ERROR, format!("$.fs: {path} is outside the workspace")));
        }
        Ok(target)
    }

    /// Diagnostics: broker log line plus a `mod.report` notify to Python.
    pub fn report(&self, line: &str) {
        eprintln!("[mod-broker] {line}");
        let frame = Frame::Notify {
            v: PROTOCOL_VERSION,
            method: "mod.report".to_string(),
            session: None,
            mod_name: None,
            args: json!({ "line": line }),
        };
        if let Some(conn) = self.runtime.read().unwrap().as_ref() {
            conn.tx.send(&frame);
        }
    }

    // ---- events ----

    /// Raise an engine event. Same-session events dispatch one at a time
    /// (design §6.1); this resolves once the chain has settled.
    pub async fn raise_event(
        self: &Arc<Self>,
        event: &str,
        session: Option<String>,
        invocation: Option<String>,
        payload: Value,
        deadline_ms: Option<u64>,
    ) -> Result<Value, RpcError> {
        let (tx, rx) = oneshot::channel();
        let lane = self.lane_for(session.as_deref().unwrap_or(""));
        let this = Arc::clone(self);
        let event = event.to_string();
        lane.send(Job {
            task: Box::pin(async move {
                let result = this.do_raise(&event, session, invocation, payload, deadline_ms).await;
                let _ = tx.send(result);
            }),
        })
        .map_err(|_| RpcError::new(CODE_ERROR, "broker is shutting down"))?;
        rx.await.map_err(|_| RpcError::new(CODE_ERROR, "event dispatch was dropped"))?
    }

    fn lane_for(self: &Arc<Self>, session: &str) -> mpsc::UnboundedSender<Job> {
        if let Some(lane) = self.lanes.lock().unwrap().get(session) {
            return lane.clone();
        }
        let (tx, mut rx) = mpsc::unbounded_channel::<Job>();
        tokio::spawn(async move {
            while let Some(job) = rx.recv().await {
                job.task.await;
            }
        });
        self.lanes.lock().unwrap().insert(session.to_string(), tx.clone());
        tx
    }

    /// The chain itself, plus tool-name aliasing and post-settle triggers
    /// (redraws, `session.end` teardown).
    async fn do_raise(
        self: &Arc<Self>,
        event: &str,
        session: Option<String>,
        invocation: Option<String>,
        mut payload: Value,
        deadline_ms: Option<u64>,
    ) -> Result<Value, RpcError> {
        // Mods see Claude Code tool names (design §15.8).
        if event == "tool.call" {
            if let Some(tool) = payload.get("tool").and_then(Value::as_str) {
                payload["tool"] = json!(self.aliases.to_mod(tool));
            }
        }
        let (hook_budget, catch_ms, session_end_ms) = {
            let config = self.config.read().unwrap();
            (config.budget_for(event), config.catch_ms, config.session_end_ms)
        };
        let shared = (event == "session.end").then(|| {
            let budget = Arc::new(Budget::new(deadline_ms.unwrap_or(session_end_ms)));
            budget.start();
            budget
        });
        let hooks = self.registry.read().unwrap().select(event, None);
        let ctx = Arc::new(ChainCtx {
            broker: Arc::clone(self),
            event: event.to_string(),
            session: session.clone(),
            invocation: invocation.unwrap_or_else(|| self.next_invocation()),
            raised_by: None,
            core: ChainCore::Identity,
            validate_tool_call: event == "tool.call",
            original: payload.clone(),
            shared,
            hook_budget_ms: deadline_ms.unwrap_or(hook_budget),
            catch_budget_ms: catch_ms,
            depth: 0,
            max_depth: ops::MAX_OP_DEPTH,
        });
        let mut result = chain::run_from(&ctx, &hooks, 0, payload).await;
        if event == "tool.call" {
            if let Ok(value) = &mut result {
                if let Some(tool) = value.get("tool").and_then(Value::as_str) {
                    let mapped = self.aliases.to_host(tool);
                    value["tool"] = json!(mapped);
                }
            }
        }
        // Post-settle triggers: bands follow the events that change them.
        match event {
            "session.end" => {
                if let Some(session) = &session {
                    self.forget_session(session).await;
                }
            }
            "session.start" | "tool.call" | "turn.complete" => {
                if let Some(session) = session {
                    let broker = Arc::clone(self);
                    tokio::spawn(async move {
                        broker.surfaces_refresh(&session).await;
                    });
                }
            }
            _ => {}
        }
        result
    }

    /// Forget a session once its `session.end` chain settled: state cleared,
    /// its timers closed and drained (design §15.4).
    pub async fn forget_session(&self, session: &str) {
        self.session_state.lock().unwrap().remove(session);
        let prefix = format!("{session}\u{0}");
        let keys: Vec<String> = self
            .timers
            .lock()
            .unwrap()
            .keys()
            .filter(|key| key.starts_with(&prefix))
            .cloned()
            .collect();
        for key in keys {
            let set = self.timers.lock().unwrap().remove(&key);
            if let Some(set) = set {
                set.close().await;
            }
        }
        self.surfaces_forget(session);
    }

    /// `$` op entry (grants, chain, unwrap) — see [`ops::invoke_op`].
    pub async fn invoke_op(
        self: &Arc<Self>,
        caller: &str,
        op: &str,
        session: Option<String>,
        args: Value,
        invocation: Option<String>,
        depth: usize,
    ) -> Result<Value, RpcError> {
        ops::invoke_op(Arc::clone(self), caller, op, session, args, invocation, depth).await
    }
}

fn registry_hooks_line(hooks: &[registry::HookDesc]) -> String {
    hooks.iter().map(registry::HookDesc::describe).collect::<Vec<_>>().join(", ")
}

/// A no-op config so a broker before its first `hello` still answers ops.
fn empty_config() -> Config {
    Config {
        node_path: String::new(),
        runner_path: String::new(),
        workspace: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
        logs_dir: default_logs_dir(),
        store_path: None,
        hook_ms: 10_000,
        prompt_edit_ms: 50,
        catch_ms: 1_000,
        session_end_ms: 1_500,
        process_timeout_ms: 30_000,
        process_max_timeout_ms: 600_000,
        band_columns: 80,
        band_rows: 8,
        mods: Vec::new(),
    }
}
