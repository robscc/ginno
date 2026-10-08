//! SurfaceTable (design §3.4, ported from DSH surfaces.ts): the band above
//! the prompt as the broker draws it for one session, plus the panes a mod
//! opened with `$.ui.open` (design §7.4). A render pass walks every mod that
//! registered a `ui.render` hook — each mod gets its own chain (its hooks
//! only), so one mod's answer can never blank another's — and then each open
//! pane (same chain, with the pane id in the input props). A mod's unchanged
//! tree keeps its generation (only the action table is refreshed); a changed
//! one bumps that mod's generation. The band lands with Python as one
//! `bands.update` carrying `{generation, tree, mods: [{mod, generation,
//! tree}]}` (`generation`/`tree` mirror the first non-empty mod for older
//! frontends); a pane lands as `mod.pane.update {id, generation, tree}`.
//! Button presses carry `<mod>:a<N>` action ids the runner minted and are
//! validated against the owning mod's band *or* pane generation. A render
//! subscribes the drawer to every `$.state` slot it read, so a later write
//! redraws — delayed a macrotask (a spawned task) and throttled to 10 passes
//! a second.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::Broker;

/// Band redraws are throttled to this many passes a second.
const MAX_RENDERS_PER_SECOND: usize = 10;

/// Panes a single session may hold open at once (design §7.4).
pub const MAX_PANES_PER_SESSION: usize = 4;

/// One mod's slice of the band: its own drawing, its own generation.
#[derive(Default)]
struct ModBandState {
    generation: u64,
    /// The serialized tree; `None` is an empty band.
    tree: Option<Value>,
    /// Action ids on the current drawing, as the runner minted them
    /// (`<mod>:a<N>`).
    actions: HashSet<String>,
    /// State slots the current drawing read.
    subscribed: HashSet<String>,
}

/// One open pane: what `$.ui.open` recorded plus the drawing fed back.
#[derive(Default)]
struct PaneState {
    title: String,
    /// The mod that opened the pane; only its hooks draw it.
    owner: String,
    generation: u64,
    tree: Option<Value>,
    actions: HashSet<String>,
    subscribed: HashSet<String>,
}

pub struct Band {
    core: std::sync::Mutex<BandCore>,
    /// `$.state` slots read during the chain in flight, attributed to the mod
    /// whose chain is running; `None` outside a render pass.
    collecting: std::sync::Mutex<Option<(String, HashSet<String>)>>,
    settled: Notify,
}

#[derive(Default)]
struct BandCore {
    /// Per-mod band drawings by mod name.
    mods: std::collections::HashMap<String, ModBandState>,
    /// Open panes in open order (`$.ui.open` order).
    panes: Vec<(String, PaneState)>,
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

    /// Redraw a session's band and panes through the mods. Concurrent calls
    /// coalesce into the pass in flight plus at most one more (DSH's refresh
    /// rule). Sessions with neither a `ui.render` hook nor an open pane are
    /// not drawn at all. Boxed: the throttle path spawns a refresh from
    /// inside a render pass, so the future type must not be recursive.
    pub fn surfaces_refresh(self: &Arc<Self>, session: &str) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        let broker = Arc::clone(self);
        let session = session.to_string();
        Box::pin(async move { broker.surfaces_refresh_inner(&session).await })
    }

    async fn surfaces_refresh_inner(self: &Arc<Self>, session: &str) {
        let has_hooks = self.registry.read().unwrap().has_hook_for("ui.render");
        let panes_open = self
            .bands
            .lock()
            .unwrap()
            .get(session)
            .map(|band| !band.core.lock().unwrap().panes.is_empty())
            .unwrap_or(false);
        if !has_hooks && !panes_open {
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

    /// One render pass: per `ui.render` mod, raise the event through that
    /// mod's chain alone (DSH gives the whole chain one answer, which would
    /// let the outermost mod blank the rest — per-mod chains keep each
    /// drawing independent); then per open pane, same chain with the pane id
    /// in the input props. Collect the state slots each chain read, diff the
    /// trees, and push new generations when they changed.
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

        let config = self.config.read().unwrap().clone();
        // Mods with a `ui.render` hook, in load order: each draws its own band.
        let render_mods: Vec<String> = {
            let registry = self.registry.read().unwrap();
            let mut names: Vec<String> = Vec::new();
            for hook in registry.select("ui.render", None) {
                if !names.contains(&hook.mod_name) {
                    names.push(hook.mod_name);
                }
            }
            names
        };

        // ---- the band: one chain per mod ----
        let mut entries: Vec<Value> = Vec::new();
        let mut any_changed = false;
        let mut first_generation = 0u64;
        let mut first_tree: Option<Value> = None;
        for mod_name in &render_mods {
            *band.collecting.lock().unwrap() = Some((mod_name.clone(), HashSet::new()));
            let result = self
                .raise_event_limited("ui.render", Some(session.to_string()), None, band_input(&config), None, Some(mod_name.clone()))
                .await;
            let subscribed = band.collecting.lock().unwrap().take().map(|(_, slots)| slots).unwrap_or_default();

            let tree = match result {
                Ok(value) => normalize_tree(&value),
                Err(e) => {
                    self.report(&format!("band render failed for {mod_name}: {}", e.message));
                    None
                }
            };
            let actions = match &tree {
                Some(tree) => collect_actions(tree),
                None => HashSet::new(),
            };
            let changed = {
                let mut core = band.core.lock().unwrap();
                let state = core.mods.entry(mod_name.clone()).or_default();
                state.subscribed = subscribed;
                if state.generation > 0 && state.tree == tree {
                    // The same drawing with possibly fresh callbacks: keep the
                    // generation, hold the new actions.
                    state.actions = actions;
                    false
                } else {
                    state.generation += 1;
                    state.tree = tree.clone();
                    state.actions = actions;
                    true
                }
            };
            any_changed |= changed;
            let core = band.core.lock().unwrap();
            let state = &core.mods[mod_name];
            if let Some(tree) = &state.tree {
                if first_tree.is_none() {
                    first_generation = state.generation;
                    first_tree = Some(tree.clone());
                }
                entries.push(json!({ "mod": mod_name, "generation": state.generation, "tree": tree }));
            }
        }
        if any_changed {
            // `generation`/`tree` mirror the first non-empty mod so an older
            // frontend (single-tree band) keeps working; `mods` is the shape
            // per-mod frontends read.
            self.runtime_notify(
                "bands.update",
                Some(session.to_string()),
                None,
                json!({ "generation": first_generation, "tree": first_tree, "mods": entries }),
            );
        }

        // ---- panes: one chain per open pane, its owner's hooks ----
        let open_panes: Vec<(String, String)> = {
            let core = band.core.lock().unwrap();
            core.panes.iter().map(|(id, pane)| (id.clone(), pane.owner.clone())).collect()
        };
        for (id, owner) in open_panes {
            let (raised, subscribed) = if render_mods.contains(&owner) {
                *band.collecting.lock().unwrap() = Some((owner.clone(), HashSet::new()));
                let input = pane_input(&config, &id);
                let raised = self
                    .raise_event_limited("ui.render", Some(session.to_string()), None, input, None, Some(owner.clone()))
                    .await;
                let subscribed = band.collecting.lock().unwrap().take().map(|(_, slots)| slots).unwrap_or_default();
                (raised, subscribed)
            } else {
                // The owner lost its `ui.render` hook (unloaded/reconfigured):
                // the pane draws empty rather than keeping a stale picture.
                (Ok(Value::Null), HashSet::new())
            };
            let tree = match raised {
                Ok(value) => normalize_tree(&value),
                Err(e) => {
                    self.report(&format!("pane {id} render failed: {}", e.message));
                    None
                }
            };
            let actions = match &tree {
                Some(tree) => collect_actions(tree),
                None => HashSet::new(),
            };
            let update = {
                let mut core = band.core.lock().unwrap();
                let Some((_, pane)) = core.panes.iter_mut().find(|(pid, _)| *pid == id) else {
                    continue;
                };
                pane.subscribed = subscribed;
                if pane.generation > 0 && pane.tree == tree {
                    pane.actions = actions;
                    None
                } else {
                    pane.generation += 1;
                    pane.tree = tree.clone();
                    pane.actions = actions;
                    Some(json!({ "id": id, "generation": pane.generation, "tree": tree }))
                }
            };
            if let Some(payload) = update {
                self.runtime_notify("mod.pane.update", Some(session.to_string()), Some(owner.clone()), payload);
            }
        }
    }

    /// Run the callback a button holds and redraw. A press from an earlier
    /// generation is ignored with a warning: the button it names may no
    /// longer exist. Validation is per mod (the action id's `<mod>:` prefix
    /// names the owner): the generation must match the mod's band drawing or
    /// one of that mod's open panes. `value` is a pane Input/Select
    /// submission riding the press frame; `None` (a plain Button press)
    /// omits the field on the runner call entirely.
    pub async fn surfaces_press(
        self: &Arc<Self>,
        session: &str,
        generation: u64,
        action_id: &str,
        value: Option<Value>,
    ) {
        let band = self.band_of(session);
        let mod_name = action_id.split(':').next().unwrap_or_default().to_string();
        let current = {
            let core = band.core.lock().unwrap();
            let in_band = core
                .mods
                .get(&mod_name)
                .map(|state| state.generation == generation && state.actions.contains(action_id))
                .unwrap_or(false);
            let in_pane = core.panes.iter().any(|(_, pane)| {
                pane.owner == mod_name && pane.generation == generation && pane.actions.contains(action_id)
            });
            let top = core.mods.get(&mod_name).map(|state| state.generation).unwrap_or(0);
            if in_band || in_pane {
                None
            } else {
                Some(top)
            }
        };
        if let Some(current) = current {
            self.report(&format!(
                "band press ignored: {action_id} of generation {generation} is not on the current drawing ({current})"
            ));
            return;
        }
        let Some(conn) = self.runner(&mod_name).await else {
            self.report(&format!("band press ignored: {action_id} names mod {mod_name}, which has no runner"));
            return;
        };
        let mut call_args = json!({ "actionId": action_id, "generation": generation });
        if let Some(value) = value {
            call_args["value"] = value;
        }
        if let Err(e) = conn
            .call(
                "ui",
                "press",
                call_args,
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

    /// Note a `$.state` read: during the reading mod's chain it subscribes
    /// that mod's band and panes.
    pub fn surfaces_state_read(&self, session: &str, slot: &str, mod_name: &str) {
        if let Some(band) = self.bands.lock().unwrap().get(session) {
            if let Some((owner, collecting)) = band.collecting.lock().unwrap().as_mut() {
                if owner == mod_name {
                    collecting.insert(slot.to_string());
                }
            }
        }
    }

    /// Note a `$.state` write: a subscribed band or pane redraws, a macrotask
    /// later (spawned), so a burst of writes coalesces into one pass.
    pub fn surfaces_state_written(self: &Arc<Self>, session: &str, slot: &str) {
        let bands = self.bands.lock().unwrap();
        let Some(band) = bands.get(session) else { return };
        let subscribed = {
            let core = band.core.lock().unwrap();
            core.mods.values().any(|state| state.subscribed.contains(slot))
                || core.panes.iter().any(|(_, pane)| pane.subscribed.contains(slot))
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

    /// Record a `$.ui.open`: upsert the pane (an open id re-titles in place),
    /// capped at [`MAX_PANES_PER_SESSION`] per session.
    pub fn surfaces_pane_open(&self, session: &str, owner: &str, id: &str, title: &str) -> Result<(), String> {
        let band = self.band_of(session);
        let mut core = band.core.lock().unwrap();
        if let Some((_, pane)) = core.panes.iter_mut().find(|(pid, _)| pid == id) {
            pane.title = title.to_string();
            pane.owner = owner.to_string();
            return Ok(());
        }
        if core.panes.len() >= MAX_PANES_PER_SESSION {
            return Err(format!("$.ui.open: at most {MAX_PANES_PER_SESSION} panes per session"));
        }
        core.panes.push((
            id.to_string(),
            PaneState { title: title.to_string(), owner: owner.to_string(), ..PaneState::default() },
        ));
        Ok(())
    }

    /// Remove a `$.ui.close`d pane; returns the owner mod when one was open.
    pub fn surfaces_pane_close(&self, session: &str, id: &str) -> Option<String> {
        let band = self.band_of(session);
        let mut core = band.core.lock().unwrap();
        let index = core.panes.iter().position(|(pid, _)| pid == id)?;
        let (_, pane) = core.panes.remove(index);
        Some(pane.owner)
    }

    /// The session's open panes, oldest first (`$.ui.panes`).
    pub fn surfaces_panes(&self, session: &str) -> Vec<Value> {
        let bands = self.bands.lock().unwrap();
        let Some(band) = bands.get(session) else { return Vec::new() };
        let core = band.core.lock().unwrap();
        core.panes
            .iter()
            .map(|(id, pane)| json!({ "id": id, "title": pane.title, "mod": pane.owner }))
            .collect()
    }

    /// Forget a session's band and panes; each open pane closes with a
    /// `mod.pane.close` notify, and no further pass renders any of it.
    pub fn surfaces_forget(&self, session: &str) {
        let band = self.bands.lock().unwrap().remove(session);
        if let Some(band) = band {
            let panes = std::mem::take(&mut band.core.lock().unwrap().panes);
            for (id, pane) in panes {
                self.runtime_notify(
                    "mod.pane.close",
                    Some(session.to_string()),
                    Some(pane.owner),
                    json!({ "id": id }),
                );
            }
        }
    }
}

/// The `ui.render` input for the band (design §7.1): no `pane` key — a mod
/// tells the two apart by `props.pane`.
fn band_input(config: &crate::Config) -> Value {
    json!({
        "component": "AbovePrompt",
        "surface": "AbovePrompt",
        "props": {
            "bodyColumns": config.band_columns,
            "hasSurvey": false,
            "isWorking": false,
            "maxRows": config.band_rows,
        },
        "viewport": { "columns": config.band_columns },
    })
}

/// The `ui.render` input for one pane (design §7.4): the band's input with
/// the pane id in `props.pane`.
fn pane_input(config: &crate::Config, pane_id: &str) -> Value {
    let mut input = band_input(config);
    input["component"] = json!("Pane");
    input["surface"] = json!("Pane");
    input["props"]["pane"] = json!(pane_id);
    input
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
