//! The middleware chain, distributed (design §3.2, behavior spec §15): runs
//! the hooks selected for one event in order, each hook's `next` reaching the
//! hooks beneath and then the engine behavior, under Claude Code's failure
//! rules — skip a hook that throws, times out, or returns no result; keep the
//! result from beneath when it had already called `next`; give a `.catch`
//! handler the chance to answer in its place; report one line per hook per
//! failure kind until the mod reloads.
//!
//! Where DSH runs hooks as in-process async closures, each hook here lives in
//! its mod's runner process: the broker sends an `event` frame and arbitrates
//! `next` frames (once per hook, cached), replies, budgets, and runner death.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::Value;
use tokio::sync::{mpsc, Notify};

use crate::budget::Budget;
use crate::protocol::{
    result_err, Frame, HookFailure, RpcError, PROTOCOL_VERSION,
};
use crate::registry::HookDesc;

/// A refused `tool.call` rewrite (`next` with a changed tool/args, P0).
pub const CODE_REWRITE_REFUSED: &str = "rewrite-refused";
/// A runner without a `.catch` handler answers a catch-call with this code.
pub const CODE_NO_CATCH: &str = "no-catch";
/// A hook (or its runner) produced no answer: `returned no result`.
pub const CODE_NO_RESULT: &str = "no-result";
/// A hook threw.
pub const CODE_HOOK_THROW: &str = "hook-throw";

/// Frame routing from runner inbound loops to the hook run awaiting them. At
/// most one hook of a given (invocation, mod) is active at a time — the chain
/// is strictly sequential — so an `Active` key names exactly one receiver.
#[derive(Hash, Eq, PartialEq, Clone, Debug)]
pub enum HubKey {
    /// Replies to an `event`/`catch-call` frame this broker sent.
    Reply { mod_name: String, frame_id: u64 },
    /// `next` frames (and synthetic notices) for the active hook run.
    Active { invocation: String, mod_name: String },
}

/// Messages the chain loop selects over.
#[derive(Clone)]
pub enum HookMsg {
    Next { frame_id: u64, e: Value },
    Reply { res: Result<Value, RpcError> },
    RunnerDead,
}

type HookTx = mpsc::UnboundedSender<HookMsg>;

#[derive(Default)]
pub struct Hub {
    entries: Mutex<HashMap<HubKey, HookTx>>,
}

impl Hub {
    pub fn insert(&self, key: HubKey, tx: HookTx) {
        self.entries.lock().unwrap().insert(key, tx);
    }

    pub fn remove(&self, key: &HubKey) {
        self.entries.lock().unwrap().remove(key);
    }

    pub fn send(&self, key: &HubKey, msg: HookMsg) -> bool {
        if let Some(tx) = self.entries.lock().unwrap().get(key) {
            return tx.send(msg).is_ok();
        }
        false
    }

    /// Deliver `msg` to every active hook run of the mod — used when its
    /// runner dies, so each in-flight hook fails and its chain continues.
    pub fn send_to_mod(&self, mod_name: &str, msg: HookMsg) {
        let entries = self.entries.lock().unwrap();
        for (key, tx) in entries.iter() {
            if let HubKey::Active { mod_name: owner, .. } = key {
                if owner == mod_name {
                    let _ = tx.send(msg.clone());
                }
            }
        }
    }
}

/// The one run beneath a hook: first `next` starts it, later calls get the
/// cached result, and every pending `next` frame is answered when it settles
/// — including frames from a `.catch` handler sharing the same run.
#[derive(Default)]
pub struct BeneathCell {
    state: Mutex<CellState>,
    settled: Notify,
}

#[derive(Default)]
struct CellState {
    started: bool,
    pending: Vec<u64>,
    result: Option<Result<Value, RpcError>>,
}

impl BeneathCell {
    /// Claim the single run beneath. `false` means one is already in flight.
    pub fn begin(&self) -> bool {
        let mut state = self.state.lock().unwrap();
        if state.started {
            return false;
        }
        state.started = true;
        true
    }

    pub fn started(&self) -> bool {
        self.state.lock().unwrap().started
    }

    pub fn cached(&self) -> Option<Result<Value, RpcError>> {
        self.state.lock().unwrap().result.clone()
    }

    pub fn add_pending(&self, frame_id: u64) {
        self.state.lock().unwrap().pending.push(frame_id);
    }

    /// Store the settled result; returns the `next` frame ids to answer.
    pub fn settle(&self, result: Result<Value, RpcError>) -> Vec<u64> {
        let mut state = self.state.lock().unwrap();
        state.result = Some(result);
        std::mem::take(&mut state.pending)
    }

    pub async fn wait_settled(&self) {
        while self.cached().is_none() {
            self.settled.notified().await;
        }
    }

    pub fn notify_settled(&self) {
        self.settled.notify_waiters();
    }
}

/// The engine behavior at the bottom of the chain.
#[derive(Clone)]
pub enum ChainCore {
    /// An engine event: identity — the final input is the event's result.
    Identity,
    /// A `$` call: run the op implementation, answering `{value}` or `{deny}`.
    Op { op: String, caller: String },
}

/// Everything one dispatch needs besides the hooks themselves.
pub struct ChainCtx {
    pub broker: Arc<crate::Broker>,
    pub event: String,
    pub session: Option<String>,
    pub invocation: String,
    /// The raising mod's load order for `$`-raised calls; `None` for engine events.
    pub raised_by: Option<usize>,
    pub core: ChainCore,
    /// P0 `tool.call` rule: a `next` that changes tool or args is refused.
    pub validate_tool_call: bool,
    pub original: Value,
    /// The `session.end` whole-chain cap: every hook shares one budget.
    pub shared: Option<Arc<Budget>>,
    pub hook_budget_ms: u64,
    pub catch_budget_ms: u64,
    /// `$`-raised calls nested inside `$`-raised calls; capped defensively.
    pub depth: usize,
    /// Max `$`-in-`$` nesting before the broker refuses (DSH has no cap; this
    /// only stops pathological infinite middleware recursion).
    pub max_depth: usize,
}

/// One classified hook failure: `throw`, `timeout`, or the no-result skip.
pub struct Failure {
    pub kind: String,
    pub message: String,
    pub line: String,
}

impl Failure {
    fn throw(message: String) -> Failure {
        let line = format!("threw {message}");
        Failure { kind: "throw".to_string(), message, line }
    }

    fn no_result() -> Failure {
        Failure {
            kind: "throw".to_string(),
            message: "returned no result".to_string(),
            line: "returned no result".to_string(),
        }
    }

    fn timeout(limit_ms: u64) -> Failure {
        Failure {
            kind: "timeout".to_string(),
            message: format!("ran past its {limit_ms} ms limit"),
            line: format!("timeout, ran past its {limit_ms} ms limit"),
        }
    }
}

enum Verdict {
    Answered(Value),
    Failed(Failure),
}

/// One hook's run: everything the answer loop shares across hook and catch
/// phases.
struct HookRun {
    ctx: Arc<ChainCtx>,
    hooks: Vec<HookDesc>,
    index: usize,
    input: Value,
    mod_name: String,
    conn: Arc<crate::runner::RunnerConn>,
    cell: Arc<BeneathCell>,
    abandoned: Arc<AtomicBool>,
}

fn result_undefined(frame_id: u64) -> Frame {
    Frame::Result {
        v: PROTOCOL_VERSION,
        id: frame_id,
        ok: true,
        value: None,
        code: None,
        message: None,
    }
}

/// P0 `tool.call` rewrite check (design §15.5): the logged call runs as
/// logged. Rewrites are P1; for now they are refused with DSH's wording.
fn validate_tool_call(next_e: &Value, original: &Value) -> Option<String> {
    let name_of = |value: Option<&Value>| match value {
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
        None => "(none)".to_string(),
    };
    let original_tool = original.get("tool");
    let next_tool = next_e.get("tool");
    if original_tool != next_tool {
        return Some(format!("rerouted the call from {} to {}", name_of(original_tool), name_of(next_tool)));
    }
    if original.get("args") != next_e.get("args") {
        return Some(format!("rewrote the arguments of {}", name_of(original_tool)));
    }
    None
}

/// Run the chain from hook `index` on, with `input` as the event input.
/// Boxed: `run_from` → `run_hook` → `run_from` is genuinely recursive (each
/// hook's `next` runs the rest of the chain beneath it).
pub fn run_from<'a>(
    ctx: &'a Arc<ChainCtx>,
    hooks: &'a [HookDesc],
    index: usize,
    input: Value,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Value, RpcError>> + Send + 'a>> {
    Box::pin(run_from_inner(ctx, hooks, index, input))
}

async fn run_from_inner(ctx: &Arc<ChainCtx>, hooks: &[HookDesc], index: usize, input: Value) -> Result<Value, RpcError> {
    for i in index..hooks.len() {
        if hooks[i].matcher_matches(&input) {
            return run_hook(ctx, hooks, i, input).await;
        }
    }
    run_core(ctx, input).await
}

async fn run_core(ctx: &Arc<ChainCtx>, input: Value) -> Result<Value, RpcError> {
    match &ctx.core {
        ChainCore::Identity => {
            // A pass-through `ui.render` hook ("draw nothing") settles the
            // band empty — the render input itself is not a tree (DSH's host
            // core behaves the same way).
            if ctx.event == "ui.render" {
                Ok(Value::Null)
            } else {
                Ok(input)
            }
        }
        // DSH's invoke wraps the op's answer as `{ value }` (or `{ deny }`
        // via OpDenied); hooks answer in the same shape.
        ChainCore::Op { op, caller } => {
            let value = crate::ops::op_core(ctx, op, caller, input).await?;
            Ok(serde_json::json!({ "value": value }))
        }
    }
}

/// The broker's report line for a skipped hook, deduped per hook per kind
/// until the mod reloads.
fn report_hook_failure(ctx: &ChainCtx, hook: &HookDesc, failure: &Failure) {
    let mut reported = hook.reported.lock().unwrap();
    if reported.contains(&failure.kind) {
        return;
    }
    reported.insert(failure.kind.clone());
    drop(reported);
    let line = format!("{}: {} hook skipped: {}", hook.mod_name, ctx.event, failure.line);
    ctx.broker.report(&line);
}

async fn run_hook(ctx: &Arc<ChainCtx>, hooks: &[HookDesc], index: usize, input: Value) -> Result<Value, RpcError> {
    let hook = hooks[index].clone();
    let mod_name = hook.mod_name.clone();
    let broker = &ctx.broker;
    let Some(conn) = broker.runner(&mod_name).await else {
        // The mod lost its runner between selection and dispatch: it fails
        // like any dead-runner hook and the chain continues.
        let failure = Failure::throw("runner dead".to_string());
        report_hook_failure(ctx, &hook, &failure);
        return run_from(ctx, hooks, index + 1, input).await;
    };

    // Per-hook budget, capped by the shared `session.end` clock when one runs.
    let mut limit_ms = ctx.hook_budget_ms;
    if let Some(shared) = &ctx.shared {
        limit_ms = limit_ms.min(shared.remaining_ms().max(1));
    }
    let budget = Arc::new(Budget::new(limit_ms));
    let budget_key = (ctx.invocation.clone(), mod_name.clone());
    broker.budgets.lock().unwrap().insert(budget_key.clone(), budget.clone());

    let run = Arc::new(HookRun {
        ctx: ctx.clone(),
        hooks: hooks.to_vec(),
        index,
        input,
        mod_name: mod_name.clone(),
        conn: conn.clone(),
        cell: Arc::new(BeneathCell::default()),
        abandoned: Arc::new(AtomicBool::new(false)),
    });

    let (tx, mut rx) = mpsc::unbounded_channel::<HookMsg>();
    let event_frame_id = conn.next_id();
    let reply_key = HubKey::Reply { mod_name: mod_name.clone(), frame_id: event_frame_id };
    let active_key = HubKey::Active { invocation: ctx.invocation.clone(), mod_name: mod_name.clone() };
    broker.hub.insert(reply_key.clone(), tx.clone());
    broker.hub.insert(active_key.clone(), tx.clone());

    conn.send(&Frame::Event {
        v: PROTOCOL_VERSION,
        id: event_frame_id,
        event: ctx.event.clone(),
        session: ctx.session.clone(),
        invocation: ctx.invocation.clone(),
        payload: run.input.clone(),
        deadline_ms: Some(budget.limit_ms()),
        hook: Some(hook.index_in_mod),
    });
    budget.start();

    let verdict = answer_loop(run.clone(), budget.clone(), &mut rx, Phase::Hook).await;
    // From here the hook gets no further runs beneath: a late `next` answers
    // undefined.
    run.abandoned.store(true, Ordering::SeqCst);

    budget.stop();
    broker.budgets.lock().unwrap().remove(&budget_key);

    match verdict {
        Verdict::Answered(value) => {
            // The hook answered; whatever it started beneath still runs to its
            // end before the event settles — a failure there is reported, but
            // the hook's answer stands.
            if run.cell.started() {
                run.cell.wait_settled().await;
                if let Some(Err(beneath)) = run.cell.cached() {
                    let line = format!(
                        "{}: {}: the chain beneath failed after the hook answered: {}",
                        mod_name, ctx.event, beneath.message
                    );
                    broker.report(&line);
                }
            }
            broker.hub.remove(&reply_key);
            broker.hub.remove(&active_key);
            Ok(value)
        }
        Verdict::Failed(failure) => {
            let beneath_error = if run.cell.started() {
                run.cell.wait_settled().await;
                run.cell.cached()
            } else {
                None
            };
            // The engine beneath failed, not the hook: the failure is the
            // event's own and propagates upward (matched by code+message,
            // the distributed stand-in for DSH's error identity check).
            if let Some(Err(beneath)) = beneath_error {
                if failure.kind == "throw" && failure.message == beneath.message {
                    broker.hub.remove(&reply_key);
                    broker.hub.remove(&active_key);
                    return Err(beneath);
                }
            }
            let catch_answered = finish_failure(ctx, hooks, index, &run.input, failure, run.clone()).await;
            broker.hub.remove(&reply_key);
            broker.hub.remove(&active_key);
            if let Some(value) = catch_answered {
                return Ok(value);
            }
            // Whatever ran beneath, for the hook or its handler, ran once; its
            // result stands (a beneath failure propagates as the event's own).
            if let Some(cached) = run.cell.cached() {
                return cached;
            }
            run_from(ctx, hooks, index + 1, run.input.clone()).await
        }
    }
}

/// The post-failure path: report (deduped per kind), then run the failed
/// hook's `.catch` handler. The handler shares the hook's beneath state, so
/// nothing beneath runs twice. Returns the handler's answer when it gave one.
async fn finish_failure(
    ctx: &Arc<ChainCtx>,
    hooks: &[HookDesc],
    index: usize,
    input: &Value,
    failure: Failure,
    run: Arc<HookRun>,
) -> Option<Value> {
    let hook = &hooks[index];
    let broker = &ctx.broker;
    let mod_name = &hook.mod_name;
    report_hook_failure(ctx, hook, &failure);

    // `.catch` phase: the handler shares the hook's beneath state.
    let conn = run.conn.clone();
    let catch_budget = Arc::new(Budget::new(ctx.catch_budget_ms));
    let budget_key = (ctx.invocation.clone(), mod_name.clone());
    broker.budgets.lock().unwrap().insert(budget_key.clone(), catch_budget.clone());

    let (tx, mut rx) = mpsc::unbounded_channel::<HookMsg>();
    let catch_frame_id = conn.next_id();
    let catch_reply_key = HubKey::Reply { mod_name: mod_name.clone(), frame_id: catch_frame_id };
    broker.hub.insert(catch_reply_key.clone(), tx.clone());
    // Re-point the active-hook routing at the catch channel: the handler's
    // `next` shares the hook's beneath state through it.
    let catch_active_key = HubKey::Active { invocation: ctx.invocation.clone(), mod_name: mod_name.clone() };
    broker.hub.insert(catch_active_key, tx.clone());
    conn.send(&Frame::CatchCall {
        v: PROTOCOL_VERSION,
        id: catch_frame_id,
        invocation: ctx.invocation.clone(),
        event: ctx.event.clone(),
        payload: input.clone(),
        failure: HookFailure { kind: failure.kind.clone(), message: failure.message.clone() },
        deadline_ms: Some(catch_budget.limit_ms()),
    });
    catch_budget.start();

    let verdict = answer_loop(run, catch_budget.clone(), &mut rx, Phase::Catch).await;
    catch_budget.stop();
    broker.budgets.lock().unwrap().remove(&budget_key);
    broker.hub.remove(&catch_reply_key);

    match verdict {
        Verdict::Answered(value) => Some(value),
        Verdict::Failed(catch_failure) => {
            if catch_failure.kind != "skipped-silent" {
                let line = format!(
                    "{}: {} .catch handler skipped: {}",
                    mod_name, ctx.event, catch_failure.line
                );
                broker.report(&line);
            }
            None
        }
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Hook,
    Catch,
}

/// Select over `next` frames, replies, and the budget until the hook (or its
/// `.catch` handler) settles or fails.
async fn answer_loop(
    run: Arc<HookRun>,
    budget: Arc<Budget>,
    rx: &mut mpsc::UnboundedReceiver<HookMsg>,
    phase: Phase,
) -> Verdict {
    let ctx = &run.ctx;
    let conn = &run.conn;
    let verdict = loop {
        tokio::select! {
            msg = rx.recv() => match msg {
                Some(HookMsg::Next { frame_id, e }) => {
                    if run.cell.started() {
                        // One run beneath per hook: a second call hands back the first run.
                        match run.cell.cached() {
                            Some(res) => conn.send_result(frame_id, &res),
                            None if phase == Phase::Hook && run.abandoned.load(Ordering::SeqCst) => {
                                conn.send(&result_undefined(frame_id));
                            }
                            None => run.cell.add_pending(frame_id),
                        }
                        continue;
                    }
                    if phase == Phase::Hook && run.abandoned.load(Ordering::SeqCst) {
                        conn.send(&result_undefined(frame_id));
                        continue;
                    }
                    // Validation precedes claiming the run: a refused next
                    // starts nothing beneath (DSH sets state.refused there).
                    if ctx.validate_tool_call {
                        if let Some(message) = validate_tool_call(&e, &ctx.original) {
                            conn.send(&result_err(frame_id, CODE_REWRITE_REFUSED, message));
                            continue;
                        }
                    }
                    run.cell.begin();
                    // This first next's reply is delivered by the beneath
                    // wrapper when the run settles (a second next joining
                    // meanwhile lands in the same pending list).
                    run.cell.add_pending(frame_id);
                    budget.pause();
                    let cell = run.cell.clone();
                    let budget2 = budget.clone();
                    let conn2 = conn.clone();
                    let ctx2 = ctx.clone();
                    let hooks2 = run.hooks.clone();
                    let index2 = run.index + 1;
                    tokio::spawn(async move {
                        let res = run_from(&ctx2, &hooks2, index2, e).await;
                        budget2.resume();
                        for frame_id in cell.settle(res.clone()) {
                            conn2.send_result(frame_id, &res);
                        }
                        cell.notify_settled();
                    });
                }
                Some(HookMsg::Reply { res }) => break classify_reply(res, phase),
                Some(HookMsg::RunnerDead) => break Verdict::Failed(Failure::throw("runner dead".to_string())),
                None => break Verdict::Failed(Failure::throw("hook channel closed".to_string())),
            },
            _ = budget.expired() => {
                // Tell the runner to stop waiting on the hook (best effort; it
                // may have settled already). A late `next` from it answers
                // undefined.
                conn.send(&Frame::Notify {
                    v: PROTOCOL_VERSION,
                    method: "hook-timeout".to_string(),
                    session: ctx.session.clone(),
                    mod_name: Some(run.mod_name.clone()),
                    args: serde_json::json!({"invocation": ctx.invocation}),
                });
                break Verdict::Failed(Failure::timeout(budget.limit_ms()));
            }
            // The `session.end` whole-chain cap: once it fires, whichever hook
            // is running fails on the shared budget.
            _ = shared_expired(ctx), if ctx.shared.is_some() => {
                let limit = ctx.shared.as_ref().unwrap().limit_ms();
                break Verdict::Failed(Failure::timeout(limit));
            }
        }
    };
    verdict
}

/// Resolves when the dispatch's shared `session.end` budget expires.
async fn shared_expired(ctx: &ChainCtx) {
    let shared = ctx.shared.as_ref().unwrap();
    shared.expired().await;
}

fn classify_reply(res: Result<Value, RpcError>, phase: Phase) -> Verdict {
    match res {
        Ok(value) => match &value {
            // `null` is an answer (a surface drawn empty) and arrays are
            // answers (serialized trees); only a hook that settles with no
            // object at all is skipped — DSH's `typeof result !== 'object'`.
            Value::Object(_) | Value::Array(_) | Value::Null => Verdict::Answered(value),
            _ => Verdict::Failed(Failure::no_result()),
        },
        Err(e) => {
            if phase == Phase::Catch && e.code == CODE_NO_CATCH {
                // The runner has no handler: fall through without a report.
                return Verdict::Failed(Failure { kind: "skipped-silent".to_string(), message: e.message, line: String::new() });
            }
            if e.code == CODE_NO_RESULT {
                return Verdict::Failed(Failure::no_result());
            }
            Verdict::Failed(Failure::throw(e.message))
        }
    }
}
