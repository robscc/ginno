//! TimerSet (design §15.4): timers a mod starts belong to the session whose
//! event started them. Closing cancels every scheduled timer, refuses new
//! ones, and waits for the callbacks already running — so a session teardown
//! (or a mod unload) cannot leave a timer touching the host afterwards.
//! Callbacks run in the runner: the broker sends a `clock.fire` call and
//! treats its result as the callback's completion.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::{mpsc, Notify};

use crate::Broker;

/// One scheduled timer: dropping the sender cancels the sleeping task.
struct TimerEntry {
    cancel: mpsc::Sender<()>,
}

pub struct TimerSet {
    /// The mod whose timers these are, named in failure reports.
    owner: String,
    session: Option<String>,
    broker: std::sync::Weak<Broker>,
    next_id: AtomicU64,
    entries: Mutex<HashMap<String, TimerEntry>>,
    closed: AtomicBool,
    running: Mutex<usize>,
    drained: Notify,
}

impl TimerSet {
    pub fn new(broker: std::sync::Weak<Broker>, session: Option<String>, owner: String) -> Arc<TimerSet> {
        Arc::new(TimerSet {
            owner,
            session,
            broker,
            next_id: AtomicU64::new(1),
            entries: Mutex::new(HashMap::new()),
            closed: AtomicBool::new(false),
            running: Mutex::new(0),
            drained: Notify::new(),
        })
    }

    /// Schedule one callback after `ms`. A closed set refuses new timers with
    /// a dead handle (DSH returns a no-op cancel there too).
    pub async fn after(self: &Arc<Self>, ms: u64) -> String {
        let id = self.register();
        if id.is_empty() {
            return id;
        }
        let this = self.clone();
        let id2 = id.clone();
        let (cancel_tx, mut cancel_rx) = mpsc::channel::<()>(1);
        self.entries.lock().unwrap().insert(id2.clone(), TimerEntry { cancel: cancel_tx });
        tokio::spawn(async move {
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_millis(ms.min(u64::MAX / 2))) => {
                    this.entries.lock().unwrap().remove(&id2);
                    this.fire(&id2).await;
                }
                _ = cancel_rx.recv() => {}
            }
        });
        id
    }

    /// Schedule one callback every `ms`, until the set closes or the handle is
    /// cancelled.
    pub async fn every(self: &Arc<Self>, ms: u64) -> String {
        let id = self.register();
        if id.is_empty() {
            return id;
        }
        let this = self.clone();
        let id2 = id.clone();
        let (cancel_tx, mut cancel_rx) = mpsc::channel::<()>(1);
        self.entries.lock().unwrap().insert(id2.clone(), TimerEntry { cancel: cancel_tx });
        let period = Duration::from_millis(ms.clamp(1, u64::MAX / 2));
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(period);
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            ticker.tick().await; // interval fires immediately; skip the first
            loop {
                tokio::select! {
                    _ = ticker.tick() => {
                        if this.closed.load(Ordering::SeqCst) {
                            break;
                        }
                        this.fire(&id2).await;
                    }
                    _ = cancel_rx.recv() => break,
                }
            }
            this.entries.lock().unwrap().remove(&id2);
        });
        id
    }

    /// Cancel one timer handle (`$.clock.cancel`).
    pub fn cancel(&self, id: &str) {
        if let Some(entry) = self.entries.lock().unwrap().remove(id) {
            let _ = entry.cancel.try_send(());
        }
    }

    /// Cancel every scheduled timer, refuse new ones, and settle once the
    /// callbacks already running have finished.
    pub async fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        let entries: Vec<TimerEntry> = { std::mem::take(&mut *self.entries.lock().unwrap()) }
            .into_values()
            .collect();
        for entry in entries {
            let _ = entry.cancel.try_send(());
        }
        while *self.running.lock().unwrap() > 0 {
            self.drained.notified().await;
        }
    }

    /// A fresh handle id, or `""` once closed (the runner shim hands back a
    /// dead handle either way).
    fn register(&self) -> String {
        if self.closed.load(Ordering::SeqCst) {
            return String::new();
        }
        format!("t{}", self.next_id.fetch_add(1, Ordering::SeqCst))
    }

    /// Run the callback in the runner and report a failure, counting the run
    /// so `close` can await it.
    async fn fire(&self, id: &str) {
        if self.closed.load(Ordering::SeqCst) {
            return;
        }
        *self.running.lock().unwrap() += 1;
        {
            let Some(broker) = self.broker.upgrade() else {
                *self.running.lock().unwrap() -= 1;
                self.drained.notify_waiters();
                return;
            };
            if let Some(conn) = broker.runner(&self.owner).await {
                if let Err(e) = conn
                    .call("clock", "fire", serde_json::json!({"timer": id}), self.session.clone(), Some(self.owner.clone()), None)
                    .await
                {
                    if e.code != crate::chain::CODE_NO_CATCH {
                        broker.report(&format!("{}: timer callback failed: {}", self.owner, e.message));
                    }
                }
            }
        }
        *self.running.lock().unwrap() -= 1;
        self.drained.notify_waiters();
    }
}
