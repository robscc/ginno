//! SurfaceTable (design §3.4, ported from DSH surfaces.ts): the band above
//! the prompt as the broker draws it for one session. A render runs the
//! `ui.render` event through the mods; the chain's answer is the serialized
//! tree, with Button presses carried by `<mod>:a<N>` action ids the runner
//! minted. An unchanged tree keeps its generation (only the action table is
//! refreshed); a changed one bumps the generation and is pushed to Python as
//! a `bands.update` notify. A render pass subscribes the band to every
//! `$.state` slot it read, so a later write redraws it — delayed a macrotask
//! (a spawned task) and throttled to 10 renders a second.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::Broker;

/// Band redraws are throttled to this many passes a second.
const MAX_RENDERS_PER_SECOND: usize = 10;

pub struct Band {
    core: std::sync::Mutex<BandCore>,
    /// `$.state` slots read during the pass in flight; `None` outside one.
    collecting: std::sync::Mutex<Option<HashSet<String>>>,
    settled: Notify,
}

#[derive(Default)]
struct BandCore {
    generation: u64,
    /// The serialized tree; `None` is an empty band.
    tree: Option<Value>,
    /// Action ids on the current drawing, as the runner minted them
    /// (`<mod>:a<N>`).
    actions: HashSet<String>,
    /// State slots the current drawing read.
    subscribed: HashSet<String>,
    rendering: bool,
    need_another: bool,
    times: std::collections::VecDeque<Instant>,
    trailing_scheduled: bool,
}

impl Band {
    fn new() -> Arc<Band> {
        Arc::new(Band {
            core: std::sync::Mutex::new(BandCore::default()),
            collecting: std::sync::Mutex::new(None),
            settled: Notify::new(),
        })
    }
}

impl Broker {
    fn band_of(&self, session: &str) -> Arc<Band> {
        if let Some(band) = self.bands.lock().unwrap().get(session) {
            return band.clone();
        }
        let band = Band::new();
        self.bands.lock().unwrap().insert(session.to_string(), band.clone());
        band
    }

    /// Redraw a session's band through the mods. Concurrent calls coalesce
    /// into the pass in flight plus at most one more (DSH's refresh rule).
    /// Only sessions a `ui.render` hook exists for are drawn at all. Boxed:
    /// the throttle path spawns a refresh from inside a render pass, so the
    /// future type must not be recursive.
    pub fn surfaces_refresh(self: &Arc<Self>, session: &str) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        let broker = Arc::clone(self);
        let session = session.to_string();
        Box::pin(async move { broker.surfaces_refresh_inner(&session).await })
    }

    async fn surfaces_refresh_inner(self: &Arc<Self>, session: &str) {
        if !self.registry.read().unwrap().has_hook_for("ui.render") {
            return;
        }
        let band = self.band_of(session);
        loop {
            let acquired = {
                let mut core = band.core.lock().unwrap();
                if core.rendering {
                    core.need_another = true;
                    None
                } else {
                    core.rendering = true;
                    Some(())
                }
            };
            if acquired.is_some() {
                break;
            }
            band.settled.notified().await;
        }
        loop {
            self.render_once(session, &band).await;
            let more = {
                let mut core = band.core.lock().unwrap();
                if core.need_another {
                    core.need_another = false;
                    true
                } else {
                    core.rendering = false;
                    false
                }
            };
            band.settled.notify_waiters();
            if !more {
                break;
            }
        }
        band.settled.notify_waiters();
    }

    /// One render pass: raise `ui.render` (through the session's lane, so it
    /// serializes with the session's events), collect the state slots it
    /// read, diff the tree, and push a new generation when it changed.
    async fn render_once(self: &Arc<Self>, session: &str, band: &Arc<Band>) {
        // Rate limit: at most 10 passes a second; throttle the rest into one
        // trailing pass after the window.
        let throttled = {
            let mut core = band.core.lock().unwrap();
            let now = Instant::now();
            while core.times.front().map(|t| now.duration_since(*t) > Duration::from_secs(1)).unwrap_or(false) {
                core.times.pop_front();
            }
            if core.times.len() >= MAX_RENDERS_PER_SECOND {
                if !core.trailing_scheduled {
                    core.trailing_scheduled = true;
                    let broker = self.clone();
                    let session = session.to_string();
                    tokio::spawn(async move {
                        tokio::time::sleep(Duration::from_millis(1100)).await;
                        let band = broker.band_of(&session);
                        {
                            let mut core = band.core.lock().unwrap();
                            core.trailing_scheduled = false;
                        }
                        broker.surfaces_refresh(&session).await;
                    });
                }
                true
            } else {
                core.times.push_back(now);
                false
            }
        };
        if throttled {
            return;
        }

        *band.collecting.lock().unwrap() = Some(HashSet::new());
        let config = self.config.read().unwrap().clone();
        let input = json!({
            "component": "AbovePrompt",
            "surface": "AbovePrompt",
            "props": {
                "bodyColumns": config.band_columns,
                "hasSurvey": false,
                "isWorking": false,
                "maxRows": config.band_rows,
            },
            "viewport": { "columns": config.band_columns },
        });
        let result = self.raise_event("ui.render", Some(session.to_string()), None, input, None).await;
        let subscribed: HashSet<String> = band.collecting.lock().unwrap().take().unwrap_or_default();

        let tree = match result {
            Ok(value) => normalize_tree(&value),
            Err(e) => {
                self.report(&format!("band render failed: {}", e.message));
                None
            }
        };
        let actions = match &tree {
            Some(tree) => collect_actions(tree),
            None => HashSet::new(),
        };

        let changed = {
            let mut core = band.core.lock().unwrap();
            core.subscribed = subscribed;
            if core.generation > 0 && core.tree == tree {
                // The same drawing with possibly fresh callbacks: keep the
                // generation, hold the new actions.
                core.actions = actions;
                false
            } else {
                core.generation += 1;
                core.tree = tree.clone();
                core.actions = actions;
                true
            }
        };
        if changed {
            let core = band.core.lock().unwrap();
            self.runtime_notify(
                "bands.update",
                Some(session.to_string()),
                None,
                json!({ "generation": core.generation, "tree": core.tree.clone() }),
            );
        }
    }

    /// Run the callback a button holds and redraw. A press from an earlier
    /// generation is ignored with a warning: the button it names may no
    /// longer exist.
    pub async fn surfaces_press(self: &Arc<Self>, session: &str, generation: u64, action_id: &str) {
        let band = self.band_of(session);
        let mod_name = {
            let core = band.core.lock().unwrap();
            if generation != core.generation || !core.actions.contains(action_id) {
                let current = core.generation;
                drop(core);
                self.report(&format!(
                    "band press ignored: {action_id} of generation {generation} is not on the current drawing ({current})"
                ));
                return;
            }
            action_id.split(':').next().unwrap_or_default().to_string()
        };
        let Some(conn) = self.runner(&mod_name).await else {
            self.report(&format!("band press ignored: {action_id} names mod {mod_name}, which has no runner"));
            return;
        };
        if let Err(e) = conn
            .call(
                "ui",
                "press",
                json!({ "actionId": action_id, "generation": generation }),
                Some(session.to_string()),
                Some(mod_name),
                None,
            )
            .await
        {
            self.report(&format!("a button's onPress failed: {}", e.message));
        }
        self.surfaces_refresh(session).await;
    }

    /// Note a `$.state` read: during a render pass it subscribes the band.
    pub fn surfaces_state_read(&self, session: &str, slot: &str) {
        if let Some(band) = self.bands.lock().unwrap().get(session) {
            if let Some(collecting) = band.collecting.lock().unwrap().as_mut() {
                collecting.insert(slot.to_string());
            }
        }
    }

    /// Note a `$.state` write: a subscribed band redraws, a macrotask later
    /// (spawned), so a burst of writes coalesces into one pass.
    pub fn surfaces_state_written(self: &Arc<Self>, session: &str, slot: &str) {
        let bands = self.bands.lock().unwrap();
        let Some(band) = bands.get(session) else { return };
        let subscribed = {
            let core = band.core.lock().unwrap();
            core.subscribed.contains(slot)
        };
        drop(bands);
        if subscribed {
            let broker = Arc::clone(self);
            let session = session.to_string();
            tokio::spawn(async move {
                broker.surfaces_refresh(&session).await;
            });
        }
    }

    /// Forget a session's band; no further pass renders it.
    pub fn surfaces_forget(&self, session: &str) {
        self.bands.lock().unwrap().remove(session);
    }
}

/// A ui.render answer as a band tree: `null` is an empty band, an array is
/// the tree, a single element object is wrapped, anything else is invalid.
fn normalize_tree(value: &Value) -> Option<Value> {
    match value {
        Value::Null => None,
        Value::Array(_) => Some(value.clone()),
        Value::Object(_) => Some(Value::Array(vec![value.clone()])),
        other => {
            let _ = other;
            None
        }
    }
}

/// Every `actionId` string in the tree (the runner mints `<mod>:a<N>`).
fn collect_actions(tree: &Value) -> HashSet<String> {
    let mut actions = HashSet::new();
    walk(tree, &mut actions);
    return actions;

    fn walk(value: &Value, actions: &mut HashSet<String>) {
        match value {
            Value::Object(fields) => {
                for (key, value) in fields {
                    if key == "actionId" {
                        if let Some(id) = value.as_str() {
                            actions.insert(id.to_string());
                        }
                    } else {
                        walk(value, actions);
                    }
                }
            }
            Value::Array(items) => {
                for item in items {
                    walk(item, actions);
                }
            }
            _ => {}
        }
    }
}
