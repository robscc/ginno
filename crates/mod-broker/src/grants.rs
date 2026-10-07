//! Grants enforcement (design §3.6): every broker-implemented capability is
//! checked here, per call, before anything runs — an unauthorized call gets
//! `{ok:false, code:"denied"}` and the capability never reaches the mod.
//!
//! Grant shapes (pushed by Python from `settings.json mods.<name>.grants`):
//! - `"fs.write": true` — workspace writes allowed.
//! - `"http.fetch": ["api.example.com", ...]` — allowed URL hostnames.
//! - `"process.run": ["/usr/bin/git", "git", ...]` — allowed `argv[0]` values.
//! - `"env.get": ["PATH", "HOME"]` — readable names; absent means the built-in
//!   read-only whitelist (`PATH`, `HOME`, anything prefixed `GINNO_`).

use serde_json::Value;

/// The env names readable without an explicit `env.get` grant.
pub const ENV_BASE_WHITELIST: &[&str] = &["PATH", "HOME"];

/// Parsed grants for one mod. Absent entries mean "not granted".
#[derive(Debug, Clone, Default)]
pub struct Grants {
    pub fs_write: bool,
    pub http_hosts: Vec<String>,
    pub process_argv0: Vec<String>,
    pub env_names: Option<Vec<String>>,
}

impl Grants {
    pub fn from_json(value: &Value) -> Grants {
        let mut grants = Grants::default();
        let Some(fields) = value.as_object() else { return grants };
        if fields.get("fs.write").and_then(Value::as_bool) == Some(true) {
            grants.fs_write = true;
        }
        grants.http_hosts = string_list(fields.get("http.fetch"));
        grants.process_argv0 = string_list(fields.get("process.run"));
        if let Some(names) = fields.get("env.get") {
            let names = string_list(Some(names));
            if !names.is_empty() {
                grants.env_names = Some(names);
            }
        }
        grants
    }

    /// `Some(reason)` when the op is not granted; `None` when it may proceed.
    /// Only security-sensitive ops are checked here; everything else passes.
    pub fn check(&self, op: &str, args: &Value) -> Option<String> {
        match op {
            "fs.write" => {
                if self.fs_write {
                    None
                } else {
                    Some("fs.write is not granted to this mod".to_string())
                }
            }
            "http.fetch" => {
                let url = args.get("url").and_then(Value::as_str).unwrap_or_default();
                match host_of(url) {
                    Some(host) if self.http_hosts.iter().any(|allowed| allowed == &host) => None,
                    Some(host) => Some(format!("http.fetch to {host} is not granted to this mod")),
                    None => Some("http.fetch needs an http(s) URL".to_string()),
                }
            }
            "process.run" => {
                let argv0 = args.get("argv").and_then(Value::as_array).and_then(|argv| argv.first()).and_then(Value::as_str);
                match argv0 {
                    Some(argv0) if self.process_argv0.iter().any(|allowed| allowed == argv0) => None,
                    Some(argv0) => Some(format!("process.run of {argv0} is not granted to this mod")),
                    None => Some("process.run needs a non-empty argv list of strings".to_string()),
                }
            }
            "env.get" => {
                let name = args.get("name").and_then(Value::as_str).unwrap_or_default();
                let allowed: Box<dyn Fn(&str) -> bool> = match &self.env_names {
                    Some(names) => Box::new(move |candidate: &str| names.iter().any(|allowed| allowed == candidate)),
                    None => Box::new(|candidate: &str| {
                        ENV_BASE_WHITELIST.contains(&candidate) || candidate.starts_with("GINNO_")
                    }),
                };
                if allowed(name) {
                    None
                } else {
                    Some(format!("env.get of {name} is not granted to this mod"))
                }
            }
            _ => None,
        }
    }
}

fn string_list(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default()
}

/// The hostname of an http(s) URL, lowercase.
fn host_of(url: &str) -> Option<String> {
    let rest = url.strip_prefix("https://").or_else(|| url.strip_prefix("http://"))?;
    let authority = rest.split(['/', '?', '#']).next()?;
    let host = authority.rsplit_once('@').map_or(authority, |(_, host)| host);
    let host = host.split_once(':').map_or(host, |(host, _)| host);
    if host.is_empty() {
        None
    } else {
        Some(host.to_ascii_lowercase())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn fs_write_needs_an_explicit_grant() {
        let granted = Grants::from_json(&json!({"fs.write": true}));
        let denied = Grants::from_json(&json!({}));
        assert!(granted.check("fs.write", &json!({})).is_none());
        assert!(denied.check("fs.write", &json!({})).is_some());
    }

    #[test]
    fn http_fetch_matches_hostnames() {
        let grants = Grants::from_json(&json!({"http.fetch": ["api.github.com"]}));
        assert!(grants.check("http.fetch", &json!({"url": "https://api.github.com/x?q=1"})).is_none());
        assert!(grants.check("http.fetch", &json!({"url": "https://evil.com/x"})).is_some());
        assert!(grants.check("http.fetch", &json!({"url": "file:///etc/passwd"})).is_some());
        assert!(Grants::default().check("http.fetch", &json!({"url": "https://api.github.com"})).is_some());
    }

    #[test]
    fn process_run_matches_argv0() {
        let grants = Grants::from_json(&json!({"process.run": ["git"]}));
        assert!(grants.check("process.run", &json!({"argv": ["git", "status"]})).is_none());
        assert!(grants.check("process.run", &json!({"argv": ["rm", "-rf"]})).is_some());
        assert!(grants.check("process.run", &json!({})).is_some());
    }

    #[test]
    fn env_get_uses_the_base_whitelist_unless_a_grant_narrows_it() {
        let base = Grants::from_json(&json!({}));
        assert!(base.check("env.get", &json!({"name": "PATH"})).is_none());
        assert!(base.check("env.get", &json!({"name": "GINNO_HOME"})).is_none());
        assert!(base.check("env.get", &json!({"name": "AWS_SECRET"})).is_some());
        let narrow = Grants::from_json(&json!({"env.get": ["AWS_ROLE"]}));
        assert!(narrow.check("env.get", &json!({"name": "AWS_ROLE"})).is_none());
        assert!(narrow.check("env.get", &json!({"name": "PATH"})).is_some());
    }

    #[test]
    fn other_ops_pass_through() {
        assert!(Grants::default().check("fs.read", &json!({})).is_none());
        assert!(Grants::default().check("state.get", &json!({})).is_none());
    }
}
