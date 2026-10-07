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

pub struct Store {
    path: Option<PathBuf>,
    data: Mutex<HashMap<String, Map<String, Value>>>,
}

impl Store {
    /// Opens (or defers opening) the store at `path`; `None` disables it.
    pub fn new(path: Option<PathBuf>) -> Store {
        Store { path, data: Mutex::new(HashMap::new()) }
    }

    fn load(&self) -> HashMap<String, Map<String, Value>> {
        let mut cached = self.data.lock().unwrap();
        if cached.is_empty() {
            if let Some(path) = &self.path {
                if let Ok(text) = std::fs::read_to_string(path) {
                    if let Ok(Value::Object(loaded)) = serde_json::from_str::<Value>(&text) {
                        for (plugin, keys) in loaded {
                            if let Value::Object(keys) = keys {
                                cached.insert(plugin, keys);
                            }
                        }
                    }
                }
            }
        }
        cached.clone()
    }

    fn save(&self, data: &HashMap<String, Map<String, Value>>) -> Result<(), String> {
        self.persist(data)?;
        *self.data.lock().unwrap() = data.clone();
        Ok(())
    }

    fn persist(&self, data: &HashMap<String, Map<String, Value>>) -> Result<(), String> {
        let Some(path) = &self.path else {
            return Err("$.store needs a configured store path".to_string());
        };
        let whole: Value = Value::Object(
            data.iter().map(|(plugin, keys)| (plugin.clone(), Value::Object(keys.clone()))).collect(),
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

    pub fn get(&self, plugin: &str, key: &str) -> Value {
        let data = self.load();
        data.get(plugin).and_then(|keys| keys.get(key).cloned()).unwrap_or(Value::Null)
    }

    pub fn set(&self, plugin: &str, key: &str, value: Value) -> Result<(), String> {
        let mut data = self.load();
        data.entry(plugin.to_string()).or_default().insert(key.to_string(), value);
        self.save(&data)
    }

    pub fn delete(&self, plugin: &str, key: &str) -> Result<(), String> {
        let mut data = self.load();
        if let Some(keys) = data.get_mut(plugin) {
            keys.remove(key);
        }
        self.save(&data)
    }

    pub fn keys(&self, plugin: &str) -> Vec<String> {
        let data = self.load();
        data.get(plugin).map(|keys| keys.keys().cloned().collect()).unwrap_or_default()
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
        let dir = std::env::temp_dir().join(format!("ginno-mod-broker-test-{name}-{}", std::process::id()));
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
            store.path.clone().unwrap()
        };
        let reopened = Store::new(Some(path));
        assert_eq!(reopened.get("a", "x"), Value::String("1".into()));
        assert_eq!(reopened.get("a", "y"), Value::Null);
        assert_eq!(reopened.keys("b"), vec!["y".to_string()]);
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
    fn deletes_keys() {
        let store = tmp_store("delete");
        store.set("a", "x", Value::Number(1.into())).unwrap();
        store.delete("a", "x").unwrap();
        assert_eq!(store.get("a", "x"), Value::Null);
        assert!(store.keys("a").is_empty());
    }
}
