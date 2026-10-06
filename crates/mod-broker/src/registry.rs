//! The hook registry: loaded mods in config (load) order and their `on(...)`
//! registrations (design §3.2). Dispatch selects hooks whose pattern selects
//! the event, outermost first; a mod's `$`-raised call is seen only by mods
//! loaded before it. Matchers are evaluated at run time against the input
//! each hook receives, not here.

use std::collections::HashSet;
use std::sync::Mutex;

use serde_json::Value;

use crate::matcher::{describe_matcher, event_matches, parse_matcher, MatcherValue, ENGINE_EVENTS, SERVED_EVENTS};

/// One `on(...)` registration, as reported by the runner's handshake.
#[derive(Debug, Clone)]
pub struct HookDesc {
    /// The mod that registered the hook.
    pub mod_name: String,
    /// The pattern passed to `on`: an exact name, `*`, or `<ns>.*`.
    pub event: String,
    /// The raw matcher JSON, for describe lines.
    pub matcher_json: Option<Value>,
    /// Parsed matcher fields, evaluated per dispatch.
    pub matcher: Option<Vec<(String, MatcherValue)>>,
    /// Ordinal within the mod, so a runner with several hooks on one event can
    /// tell dispatches apart (`hook` field on event frames).
    pub index_in_mod: u64,
    /// Failure kinds already reported for this hook: one line per kind until
    /// the mod reloads (design §15.2 report dedup).
    pub reported: std::sync::Arc<Mutex<HashSet<String>>>,
}

impl HookDesc {
    pub fn matcher_matches(&self, input: &Value) -> bool {
        match &self.matcher {
            Some(fields) => crate::matcher::matcher_matches(fields, input),
            None => true,
        }
    }

    /// The validate-style description: `tool.call{tool=Bash}`.
    pub fn describe(&self) -> String {
        format!("{}{}", self.event, describe_matcher(self.matcher_json.as_ref()))
    }
}

/// One loaded mod.
#[derive(Debug, Clone)]
pub struct ModEntry {
    pub name: String,
    pub dir: String,
    /// Position in the load order; hooks of an earlier mod run outside those
    /// of a later one.
    pub order: usize,
    /// The `hooks:` line as validate prints it, in registration order.
    pub hooks_line: String,
}

#[derive(Default)]
pub struct Registry {
    mods: Vec<ModEntry>,
    hooks: Vec<HookDesc>,
}

impl Registry {
    pub fn mods(&self) -> &[ModEntry] {
        &self.mods
    }

    pub fn hooks(&self) -> &[HookDesc] {
        &self.hooks
    }

    pub fn order_of(&self, name: &str) -> Option<usize> {
        self.mods.iter().find(|mod_entry| mod_entry.name == name).map(|mod_entry| mod_entry.order)
    }

    /// Add one mod and its registrations. A mod of the same name must be
    /// removed first (DSH wording: `another plugin of that name loads first`).
    pub fn add(&mut self, entry: ModEntry, hooks: Vec<HookDesc>) -> Result<(), String> {
        if self.order_of(&entry.name).is_some() {
            return Err(format!("hooks module {} not loaded: another plugin of that name loads first", entry.name));
        }
        let mut hooks = hooks;
        self.mods.push(entry.clone());
        for hook in hooks.iter_mut() {
            hook.mod_name = entry.name.clone();
        }
        self.hooks.extend(hooks);
        self.reindex();
        Ok(())
    }

    /// Drop one mod and its hooks; returns whether it was loaded.
    pub fn remove(&mut self, name: &str) -> bool {
        let before = self.mods.len();
        self.mods.retain(|mod_entry| mod_entry.name != name);
        self.hooks.retain(|hook| hook.mod_name != name);
        self.reindex();
        self.mods.len() != before
    }

    fn reindex(&mut self) {
        // `order` is the position in the load order; mod entries carry it.
        for (order, mod_entry) in self.mods.iter_mut().enumerate() {
            mod_entry.order = order;
        }
    }

    /// Hooks whose pattern selects `event`, outermost first. A `$` call the
    /// mod `raised_by` made is seen only by mods loaded before it.
    pub fn select(&self, event: &str, raised_by: Option<usize>) -> Vec<HookDesc> {
        let mut selected: Vec<HookDesc> = self
            .hooks
            .iter()
            .filter(|hook| {
                let order = self.order_of(&hook.mod_name).unwrap_or(usize::MAX);
                raised_by.is_none_or(|caller_order| order < caller_order) && event_matches(&hook.event, event)
            })
            .cloned()
            .collect();
        selected.sort_by_key(|hook| self.order_of(&hook.mod_name).unwrap_or(usize::MAX));
        selected
    }

    /// Restore the config's load order after out-of-order handshakes (mods
    /// load in parallel): mods and their hooks are stable-sorted by the name
    /// order given.
    pub fn sort_by(&mut self, names: &[String]) {
        let position = |name: &str| names.iter().position(|candidate| candidate == name).unwrap_or(usize::MAX);
        self.mods.sort_by_key(|mod_entry| position(&mod_entry.name));
        self.hooks.sort_by_key(|hook| position(&hook.mod_name));
        self.reindex();
    }

    /// Whether any hook would run for the event (without matcher evaluation).
    pub fn has_hook_for(&self, event: &str) -> bool {
        self.hooks.iter().any(|hook| event_matches(&hook.event, event))
    }

    /// Engine events one mod hooks by exact name that this host never raises
    /// (design §15.8 unserved report), in registration order, each once.
    pub fn unserved(&self, mod_name: &str) -> Vec<String> {
        let mut names: Vec<String> = Vec::new();
        for hook in &self.hooks {
            if hook.mod_name != mod_name
                || !hook.event.contains('.')
                || hook.event.ends_with('*')
                || SERVED_EVENTS.contains(&hook.event.as_str())
                || names.contains(&hook.event)
            {
                continue;
            }
            if !ENGINE_EVENTS.contains(&hook.event.as_str()) {
                continue;
            }
            names.push(hook.event.clone());
        }
        names
    }

    /// The validate-style `hooks:` line for one mod.
    pub fn describe(&self, mod_name: &str) -> String {
        self.hooks
            .iter()
            .filter(|hook| hook.mod_name == mod_name)
            .map(HookDesc::describe)
            .collect::<Vec<_>>()
            .join(", ")
    }
}

/// Build [`HookDesc`]s from a `hooks-registered` args payload
/// (`{"hooks": [{"event": "...", "matcher": ...}, ...]}`).
pub fn hooks_from_json(mod_name: &str, args: &Value) -> Result<Vec<HookDesc>, String> {
    let Some(hooks) = args.get("hooks").and_then(Value::as_array) else {
        return Err("hooks-registered needs {hooks: [...]}".to_string());
    };
    let mut out = Vec::with_capacity(hooks.len());
    for (index, hook) in hooks.iter().enumerate() {
        let Some(event) = hook.get("event").and_then(Value::as_str) else {
            return Err(format!("hooks[{index}] needs an event name"));
        };
        let matcher_json = hook.get("matcher").filter(|value| !value.is_null());
        let matcher = matcher_json.and_then(parse_matcher);
        if matcher_json.is_some() && matcher.is_none() {
            return Err(format!("hooks[{index}]: matcher for {event} is not a shape this broker evaluates"));
        }
        out.push(HookDesc {
            mod_name: mod_name.to_string(),
            event: event.to_string(),
            matcher_json: matcher_json.cloned(),
            matcher,
            index_in_mod: index as u64,
            reported: std::sync::Arc::new(Mutex::new(HashSet::new())),
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn hook(mod_name: &str, event: &str, matcher: Option<Value>) -> HookDesc {
        let matcher_json = matcher;
        let matcher = matcher_json.as_ref().and_then(parse_matcher);
        HookDesc {
            mod_name: mod_name.to_string(),
            event: event.to_string(),
            matcher_json: matcher_json.clone(),
            matcher,
            index_in_mod: 0,
            reported: std::sync::Arc::new(Mutex::new(HashSet::new())),
        }
    }

    #[test]
    fn selects_in_load_order_and_respects_raised_by() {
        let mut registry = Registry::default();
        registry.add(ModEntry { name: "a".into(), dir: "/a".into(), order: 0, hooks_line: String::new() },
            vec![hook("a", "tool.call", None)]).unwrap();
        registry.add(ModEntry { name: "b".into(), dir: "/b".into(), order: 1, hooks_line: String::new() },
            vec![hook("b", "tool.call", None)]).unwrap();
        registry.add(ModEntry { name: "c".into(), dir: "/c".into(), order: 2, hooks_line: String::new() },
            vec![hook("c", "tool.call", None)]).unwrap();

        let names = |hooks: &[HookDesc]| hooks.iter().map(|h| h.mod_name.clone()).collect::<Vec<_>>();
        assert_eq!(names(&registry.select("tool.call", None)), vec!["a", "b", "c"]);
        // A $ call raised by `c` is seen only by mods loaded before it.
        assert_eq!(names(&registry.select("tool.call", Some(2))), vec!["a", "b"]);
        assert!(registry.select("tool.call", Some(0)).is_empty());
    }

    #[test]
    fn remove_drops_the_mods_hooks_and_reindexes() {
        let mut registry = Registry::default();
        for name in ["a", "b", "c"] {
            registry
                .add(ModEntry { name: name.into(), dir: format!("/{name}"), order: 0, hooks_line: String::new() },
                    vec![hook(name, "tool.call", None)])
                .unwrap();
        }
        assert!(registry.remove("b"));
        let names = |hooks: &[HookDesc]| hooks.iter().map(|h| h.mod_name.clone()).collect::<Vec<_>>();
        assert_eq!(names(&registry.select("tool.call", None)), vec!["a", "c"]);
        assert_eq!(registry.order_of("c"), Some(1));
    }

    #[test]
    fn rejects_a_duplicate_name_with_dsh_wording() {
        let mut registry = Registry::default();
        registry.add(ModEntry { name: "a".into(), dir: "/a".into(), order: 0, hooks_line: String::new() }, vec![]).unwrap();
        let error = registry
            .add(ModEntry { name: "a".into(), dir: "/a2".into(), order: 1, hooks_line: String::new() }, vec![])
            .unwrap_err();
        assert_eq!(error, "hooks module a not loaded: another plugin of that name loads first");
    }

    #[test]
    fn unserved_lists_engine_events_this_host_never_raises() {
        let mut registry = Registry::default();
        registry
            .add(
                ModEntry { name: "a".into(), dir: "/a".into(), order: 0, hooks_line: String::new() },
                vec![hook("a", "tool.describe", None), hook("a", "tool.call", None), hook("a", "tool.describe", None), hook("a", "session.messages", None)],
            )
            .unwrap();
        assert_eq!(registry.unserved("a"), vec!["tool.describe".to_string()]);
    }

    #[test]
    fn parses_hooks_registered_payload() {
        let hooks = hooks_from_json(
            "a",
            &json!({"hooks": [{"event": "tool.call", "matcher": {"tool": "Bash"}}, {"event": "*"}]}),
        )
        .unwrap();
        assert_eq!(hooks.len(), 2);
        assert_eq!(hooks[0].describe(), "tool.call{tool=Bash}");
        assert_eq!(hooks[1].describe(), "*");
        assert!(hooks_from_json("a", &json!({})).is_err());
    }
}
