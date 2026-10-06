//! BudgetClock (design §15.1): accounts a hook's own running time. The clock
//! runs while the hook is busy and pauses while it awaits `next` or a mods
//! API call — both are broker round-trips, so the broker sees every pause.
//! `$.clock.sleep` is the one exception: the broker implements it itself and
//! simply does not pause for it, so it counts as the hook's own time.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::{watch, Notify};

struct State {
    spent: Duration,
    busy_since: Option<Instant>,
    stopped: bool,
    expired: bool,
}

/// Shared, cloneable busy-time clock. `pause`/`resume` nest, because a hook
/// may hold several `$` calls in flight at once.
#[derive(Clone)]
pub struct Budget {
    limit: Duration,
    state: Arc<Mutex<State>>,
    changes: Arc<Notify>,
    expired_tx: Arc<watch::Sender<bool>>,
    pause_depth: Arc<AtomicUsize>,
}

impl Budget {
    pub fn new(limit_ms: u64) -> Budget {
        let (expired_tx, _) = watch::channel(false);
        Budget {
            limit: Duration::from_millis(limit_ms),
            state: Arc::new(Mutex::new(State { spent: Duration::ZERO, busy_since: None, stopped: false, expired: false })),
            changes: Arc::new(Notify::new()),
            expired_tx: Arc::new(expired_tx),
            pause_depth: Arc::new(AtomicUsize::new(0)),
        }
    }

    /// The configured limit in whole milliseconds.
    pub fn limit_ms(&self) -> u64 {
        self.limit.as_millis() as u64
    }

    /// Milliseconds left before the deadline fires, computed at read time.
    pub fn remaining_ms(&self) -> u64 {
        let state = self.state.lock().unwrap();
        let busy = state.busy_since.map(|since| since.elapsed()).unwrap_or(Duration::ZERO);
        self.limit.saturating_sub(state.spent + busy).as_millis() as u64
    }

    /// Start counting; the hook is busy from now on. Arms the expiry driver.
    pub fn start(&self) {
        let mut state = self.state.lock().unwrap();
        if state.stopped || state.busy_since.is_some() {
            return;
        }
        state.busy_since = Some(Instant::now());
        drop(state);
        self.changes.notify_one();
        self.spawn_driver();
    }

    /// Stop counting while the hook awaits a broker round-trip. Nested pauses count.
    pub fn pause(&self) {
        if self.pause_depth.fetch_add(1, Ordering::SeqCst) > 0 {
            return;
        }
        let mut state = self.state.lock().unwrap();
        if state.stopped {
            return;
        }
        if let Some(since) = state.busy_since.take() {
            state.spent += since.elapsed();
        }
        drop(state);
        self.changes.notify_one();
    }

    /// Resume counting after the awaited call settled.
    pub fn resume(&self) {
        if self.pause_depth.fetch_sub(1, Ordering::SeqCst) != 1 {
            return;
        }
        let mut state = self.state.lock().unwrap();
        if state.stopped {
            return;
        }
        state.busy_since = Some(Instant::now());
        drop(state);
        self.changes.notify_one();
    }

    /// The hook settled: no deadline can fire from now on.
    pub fn stop(&self) {
        let mut state = self.state.lock().unwrap();
        if let Some(since) = state.busy_since.take() {
            state.spent += since.elapsed();
        }
        state.stopped = true;
        drop(state);
        self.changes.notify_one();
    }

    /// Whether the clock has fired.
    pub fn is_expired(&self) -> bool {
        self.state.lock().unwrap().expired
    }

    /// Resolves once the busy time reaches the limit. Never fires after `stop`.
    pub async fn expired(&self) {
        let mut rx = self.expired_tx.subscribe();
        while !*rx.borrow_and_update() {
            if rx.changed().await.is_err() {
                return;
            }
        }
    }

    /// Mark expired and wake everyone waiting on [`Budget::expired`].
    fn fire_expired(&self) {
        let mut state = self.state.lock().unwrap();
        if state.stopped {
            return;
        }
        state.expired = true;
        if let Some(since) = state.busy_since.take() {
            state.spent += since.elapsed();
        }
        state.spent = self.limit;
        drop(state);
        // send_replace, not send: at fire time there may be no receiver yet,
        // and the value must still be stored for later waiters.
        self.expired_tx.send_replace(true);
    }

    /// One task sleeps until the deadline would pass while busy, re-arming
    /// itself whenever the busy/paused state changes.
    fn spawn_driver(&self) {
        let this = self.clone();
        tokio::spawn(async move {
            loop {
                let (busy_until, expired, stopped) = {
                    let state = this.state.lock().unwrap();
                    (state.busy_since, state.expired, state.stopped)
                };
                if expired || stopped {
                    return;
                }
                match busy_until {
                    Some(since) => {
                        let remaining = this.limit.saturating_sub(state_spent(&this) + since.elapsed());
                        tokio::select! {
                            _ = tokio::time::sleep(remaining) => {
                                let still_busy = this.state.lock().unwrap().busy_since.is_some();
                                if still_busy {
                                    this.fire_expired();
                                    return;
                                }
                            }
                            _ = this.changes.notified() => {}
                        }
                    }
                    None => this.changes.notified().await,
                }
            }
        });
    }
}

fn state_spent(budget: &Budget) -> Duration {
    budget.state.lock().unwrap().spent
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn counts_only_busy_time_and_pauses_nested() {
        let clock = Budget::new(100);
        assert_eq!(clock.remaining_ms(), 100);
        clock.start();
        clock.pause();
        clock.pause();
        // While paused the deadline cannot fire even though the limit is tiny
        // relative to this wait.
        tokio::select! {
            _ = clock.expired() => panic!("expired while paused"),
            _ = tokio::time::sleep(Duration::from_millis(30)) => {}
        }
        // Remaining time barely moved while paused.
        assert!(clock.remaining_ms() > 60, "pause must freeze the clock: {}", clock.remaining_ms());
        clock.resume();
        clock.resume();
        clock.stop();
        clock.start();
        clock.pause();
        clock.resume();
        assert!(!clock.is_expired());
    }

    #[tokio::test]
    async fn fires_once_busy_time_reaches_the_limit() {
        let clock = Budget::new(20);
        clock.start();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(clock.is_expired());
        assert_eq!(clock.remaining_ms(), 0);
        clock.expired().await;
    }

    #[tokio::test]
    async fn resume_without_pause_is_a_noop_and_stop_silences_the_deadline() {
        let clock = Budget::new(10);
        clock.resume();
        assert_eq!(clock.remaining_ms(), 10);
        clock.stop();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!clock.is_expired());
    }
}
