//! `$.store`: one durable JSON object of keys per plugin, machine-wide under
//! the official 4 MiB cap, persisted at the path Python pushes (default
//! `~/.ginno/mods/store.json`). Writes are serialized behind one mutex —
//! per-plugin chains follow from that — and land atomically (tmp + rename).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{Map, Value};

/// Largest JSON the whole store may hold, Claude Code's own limit.
pub const STORE_MAX_BYTES: usize = 4 * 1024 * 1024;

/// A write refused because it would push the store past the cap.
pub const STORE_LIMIT_MESSAGE: &str = "$.store.set: the store would exceed 4194304 bytes of JSON";

struct StoreSnapshot {
    path: Option<PathBuf>,
    data: HashMap<String, Map<String, Value>>,
    loaded: bool,
}

pub struct Store {
    // One snapshot lock keeps path swaps and write commits consistent:
    // a save can never be cached under a different path after a config push.
    data: Mutex<StoreSnapshot>,
}

impl Store {
    /// Opens (or defers opening) the store at `path`; `None` disables it.
    pub fn new(path: Option<PathBuf>) -> Store {
        Store {
            data: Mutex::new(StoreSnapshot {
                path,
                data: HashMap::new(),
                loaded: false,
            }),
        }
    }

    /// Apply the runtime-pushed path without discarding cached data when the
    /// path is unchanged (the usual hello/reconnect/config-refresh case).
    /// Returns whether the path changed.
    pub fn set_path(&self, path: Option<PathBuf>) -> bool {
        let mut snapshot = self.data.lock().unwrap();
        if snapshot.path == path {
            return false;
        }
        snapshot.path = path;
        snapshot.data.clear();
        snapshot.loaded = false;
        true
    }

    fn load(snapshot: &mut StoreSnapshot) {
        if snapshot.loaded {
            return;
        }
        // A missing/unreadable file is an empty snapshot until the path is
        // changed; do not resurrect stale disk contents after an empty read.
        snapshot.loaded = true;
        if let Some(path) = &snapshot.path {
            if let Ok(text) = std::fs::read_to_string(path) {
                if let Ok(Value::Object(loaded)) = serde_json::from_str::<Value>(&text) {
                    for (plugin, keys) in loaded {
                        if let Value::Object(keys) = keys {
                            snapshot.data.insert(plugin, keys);
                        }
                    }
                }
            }
        }
    }
}

fn persist(path: Option<&Path>, data: &HashMap<String, Map<String, Value>>) -> Result<(), String> {
    let Some(path) = path else {
        return Err("$.store needs a configured store path".to_string());
    };
    let whole: Value = Value::Object(
        data.iter()
            .map(|(plugin, keys)| (plugin.clone(), Value::Object(keys.clone())))
            .collect(),
    );
    let encoded = serde_json::to_vec(&whole).map_err(|e| format!("$.store.set: {e}"))?;
    if encoded.len() > STORE_MAX_BYTES {
        return Err(STORE_LIMIT_MESSAGE.to_string());
    }
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, encoded).map_err(|e| format!("$.store.set: {e}"))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("$.store.set: {e}"))?;
    Ok(())
}

impl Store {
    pub fn get(&self, plugin: &str, key: &str) -> Value {
        let mut snapshot = self.data.lock().unwrap();
        let value = snapshot
            .data
            .get(plugin)
            .and_then(|keys| keys.get(key).cloned());
        if let Some(value) = value {
            return value;
        }
        Self::load(&mut snapshot);
        snapshot
            .data
            .get(plugin)
            .and_then(|keys| keys.get(key).cloned())
            .unwrap_or(Value::Null)
    }

    pub fn set(&self, plugin: &str, key: &str, value: Value) -> Result<(), String> {
        let mut snapshot = self.data.lock().unwrap();
        Self::load(&mut snapshot);
        let mut next = snapshot.data.clone();
        next.entry(plugin.to_string())
            .or_default()
            .insert(key.to_string(), value);
        // Commit only after the durable write succeeds. A refused write must
        // not become visible through get()/keys() despite being absent on disk.
        persist(snapshot.path.as_deref(), &next)?;
        snapshot.data = next;
        Ok(())
    }

    pub fn delete(&self, plugin: &str, key: &str) -> Result<(), String> {
        let mut snapshot = self.data.lock().unwrap();
        Self::load(&mut snapshot);
        let mut next = snapshot.data.clone();
        if let Some(keys) = next.get_mut(plugin) {
            keys.remove(key);
        }
        persist(snapshot.path.as_deref(), &next)?;
        snapshot.data = next;
        Ok(())
    }

    pub fn keys(&self, plugin: &str) -> Vec<String> {
        let mut snapshot = self.data.lock().unwrap();
        Self::load(&mut snapshot);
        snapshot
            .data
            .get(plugin)
            .map(|keys| keys.keys().cloned().collect())
            .unwrap_or_default()
    }
}

/// The path a broker-wide store file lives at, from a config value.
pub fn store_path_from(config_value: Option<&str>, mods_dir: &Path) -> Option<PathBuf> {
    match config_value {
        Some(path) => Some(PathBuf::from(shellexpand_home(path))),
        None => Some(mods_dir.join("store.json")),
    }
}

fn shellexpand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return Path::new(&home).join(rest).to_string_lossy().into_owned();
        }
    }
    path.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_store(name: &str) -> Store {
        let dir = std::env::temp_dir().join(format!(
            "ginno-mod-broker-test-{name}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        Store::new(Some(dir.join("store.json")))
    }

    #[test]
    fn persists_across_reopen_and_hides_other_plugins() {
        let path = {
            let store = tmp_store("persist");
            store.set("a", "x", Value::String("1".into())).unwrap();
            store.set("b", "y", Value::Bool(true)).unwrap();
            let p = store.data.lock().unwrap().path.clone().unwrap();
            p
        };
        let reopened = Store::new(Some(path));
        assert_eq!(reopened.get("a", "x"), Value::String("1".into()));
        assert_eq!(reopened.get("a", "y"), Value::Null);
        assert_eq!(reopened.keys("b"), vec!["y".to_string()]);
    }

    #[test]
    fn a_path_switch_never_exposes_the_previous_store() {
        use std::sync::{Arc, Barrier};

        // 100 次足够捕捉 set_path/写者竞态回归（b7bd68c #23）：竞态靠
        // 调度多样性暴露，不靠海量重复——2000 次只是白烧 CI 十几秒。
        for iteration in 0..100 {
            let store = Arc::new(tmp_store("path-switch-race"));
            store.set("from-a", "marker", Value::Bool(true)).unwrap();
            let second = store
                .data
                .lock()
                .unwrap()
                .path
                .clone()
                .unwrap()
                .with_file_name("other.json");
            let barrier = Arc::new(Barrier::new(2));
            let writer_store = Arc::clone(&store);
            let writer_barrier = Arc::clone(&barrier);
            let writer = std::thread::spawn(move || {
                writer_barrier.wait();
                writer_store
                    .set("writer", "value", Value::Number(iteration.into()))
                    .unwrap();
            });
            barrier.wait();
            assert!(store.set_path(Some(second)));
            writer.join().unwrap();
            assert_eq!(
                store.get("from-a", "marker"),
                Value::Null,
                "old store data leaked after the path switch at iteration {iteration}"
            );
        }
    }

    #[test]
    fn configuring_the_same_path_preserves_cached_values() {
        let store = tmp_store("reconnect");
        let path = store.data.lock().unwrap().path.clone();
        store.set("a", "x", Value::Number(1.into())).unwrap();
        assert!(!store.set_path(path));
        assert_eq!(store.get("a", "x"), Value::Number(1.into()));
    }

    #[test]
    fn changing_the_path_loads_the_new_store() {
        let store = tmp_store("change");
        let first = store.data.lock().unwrap().path.clone().unwrap();
        let second = first.with_file_name("other.json");
        store.set("a", "x", Value::Number(1.into())).unwrap();
        assert!(store.set_path(Some(second.clone())));
        assert_eq!(store.get("a", "x"), Value::Null);
        store.set("a", "x", Value::Number(2.into())).unwrap();
        assert!(store.set_path(Some(first)));
        assert_eq!(store.get("a", "x"), Value::Number(1.into()));
        assert!(store.set_path(Some(second)));
        assert_eq!(store.get("a", "x"), Value::Number(2.into()));
    }

    #[test]
    fn refuses_writes_past_the_machine_wide_cap() {
        let store = tmp_store("cap");
        let big = Value::String("x".repeat(STORE_MAX_BYTES));
        let error = store.set("a", "big", big).unwrap_err();
        assert_eq!(error, STORE_LIMIT_MESSAGE);
        assert_eq!(store.get("a", "big"), Value::Null);
    }

    #[test]
    fn failed_writes_do_not_replace_the_cached_or_durable_value() {
        let store = tmp_store("failed-write");
        store.set("a", "x", Value::Number(1.into())).unwrap();
        let big = Value::String("x".repeat(STORE_MAX_BYTES));
        store.set("a", "x", big).unwrap_err();
        assert_eq!(store.get("a", "x"), Value::Number(1.into()));

        store.set_path(None);
        assert_eq!(
            store.set("a", "x", Value::Number(2.into())).unwrap_err(),
            "$.store needs a configured store path"
        );
        assert_eq!(store.get("a", "x"), Value::Null);
    }

    #[test]
    fn an_empty_cache_does_not_reload_stale_disk_contents() {
        let store = tmp_store("empty");
        let path = store.data.lock().unwrap().path.clone().unwrap();
        std::fs::write(&path, r#"{"a":{"x":1}}"#).unwrap();
        assert_eq!(store.keys("a"), vec!["x".to_string()]);

        store.delete("a", "x").unwrap();
        store.set("b", "y", Value::Number(2.into())).unwrap();
        assert_eq!(store.get("a", "x"), Value::Null);
        let persisted: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(persisted, serde_json::json!({"a": {}, "b": {"y": 2}}));
    }
}
