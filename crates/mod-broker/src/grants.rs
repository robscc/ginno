//! Grants enforcement (design §3.6): every broker-implemented capability is
//! checked here, per call, before anything runs — an unauthorized call gets
//! `{ok:false, code:"denied"}` and the capability never reaches the mod.
//!
//! Grant shapes (pushed by Python from `settings.json mods.<name>.grants`):
//! - `"fs.write": true` — workspace writes allowed.
//! - `"http.fetch": ["api.example.com", "*.example.com", ...]` — allowed URL
//!   hostnames. `*.example.com` matches `example.com` itself and any
//!   subdomain of it (at label boundaries only: `*.example.com` never matches
//!   `evilexample.com`); a bare `*` matches any host.
//! - `"process.run": ["/usr/bin/git", "git", "brew *", ...]` — an entry
//!   matches either exactly against `argv[0]`, or, when it ends with `*`, as
//!   a prefix of the whole command line (`argv` joined with single spaces):
//!   `"brew *"` allows `["brew", "install", "gcc"]` (command line
//!   `brew install gcc`) but not `["brewy", "x"]`. A trailing `*` never
//!   demands the space back: `"git*"` also matches an argv0 like
//!   `github-cli` — write `"git *"` (or plain `"git"`) to stay on word
//!   boundaries. There is no other wildcard syntax; a process call without
//!   any `process.run` grant is denied outright.
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
                    Some(host) if self.http_hosts.iter().any(|allowed| host_allowed(allowed, &host)) => None,
                    Some(host) => Some(format!("http.fetch to {host} is not granted to this mod")),
                    None => Some("http.fetch needs an http(s) URL".to_string()),
                }
            }
            "process.run" => {
                let argv = args.get("argv").and_then(Value::as_array);
                let argv0 = argv.and_then(|argv| argv.first()).and_then(Value::as_str);
                match (argv0, argv) {
                    (Some(argv0), Some(argv)) if self.process_argv0.iter().any(|allowed| argv0_allowed(allowed, argv0, argv)) => {
                        None
                    }
                    (Some(argv0), Some(_)) => Some(format!("process.run of {argv0} is not granted to this mod")),
                    _ => Some("process.run needs a non-empty argv list of strings".to_string()),
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

/// Does one `http.fetch` grant entry allow `host`? Exact, or `*.domain`
/// covering the domain and its subdomains at label boundaries, or a bare `*`
/// for any host. (See the module docs for the reasoning.)
fn host_allowed(entry: &str, host: &str) -> bool {
    if let Some(suffix) = entry.strip_prefix("*.") {
        host == suffix || host.strip_suffix(suffix).is_some_and(|prefix| prefix.ends_with('.'))
    } else {
        entry == "*" || entry == host
    }
}

/// Does one `process.run` grant entry allow this call? Exact `argv[0]` match,
/// or a trailing `*` as a prefix of the whole command line (see module docs).
fn argv0_allowed(entry: &str, argv0: &str, argv: &[Value]) -> bool {
    if let Some(prefix) = entry.strip_suffix('*') {
        let command_line = std::iter::once(argv0.to_string())
            .chain(argv.iter().skip(1).filter_map(Value::as_str).map(str::to_string))
            .collect::<Vec<_>>()
            .join(" ");
        command_line.starts_with(prefix)
    } else {
        entry == argv0
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
    fn http_fetch_wildcards_cover_a_domain_and_its_subdomains() {
        let grants = Grants::from_json(&json!({"http.fetch": ["*.example.com"]}));
        assert!(grants.check("http.fetch", &json!({"url": "https://api.example.com/v1"})).is_none());
        assert!(grants.check("http.fetch", &json!({"url": "https://example.com"})).is_none(), "the apex too");
        // Only at label boundaries: a lookalike host is not covered.
        assert!(grants.check("http.fetch", &json!({"url": "https://evilexample.com"})).is_some());
        assert!(grants.check("http.fetch", &json!({"url": "https://notexample.com"})).is_some());
        // A bare `*` allows every host.
        let any = Grants::from_json(&json!({"http.fetch": ["*"]}));
        assert!(any.check("http.fetch", &json!({"url": "https://anything.dev"})).is_none());
    }

    #[test]
    fn process_run_matches_argv0() {
        let grants = Grants::from_json(&json!({"process.run": ["git"]}));
        assert!(grants.check("process.run", &json!({"argv": ["git", "status"]})).is_none());
        assert!(grants.check("process.run", &json!({"argv": ["rm", "-rf"]})).is_some());
        assert!(grants.check("process.run", &json!({})).is_some());
    }

    #[test]
    fn process_run_wildcards_match_the_command_line_prefix() {
        // `"brew *"`: a prefix of the whole command line, so subcommands and
        // their arguments ride along — but a different binary does not.
        let grants = Grants::from_json(&json!({"process.run": ["brew *"]}));
        assert!(grants.check("process.run", &json!({"argv": ["brew", "install", "gcc"]})).is_none());
        assert!(grants.check("process.run", &json!({"argv": ["brewy", "x"]})).is_some(), "prefix stops at the binary name");
        // `"git*"` reaches past the binary name too (documented sharp edge).
        let loose = Grants::from_json(&json!({"process.run": ["git*"]}));
        assert!(loose.check("process.run", &json!({"argv": ["github-cli", "auth"]})).is_none());
        // An exact entry matches argv0 only — any subcommand rides along
        // (pre-existing semantics, kept).
        let exact = Grants::from_json(&json!({"process.run": ["git"]}));
        assert!(exact.check("process.run", &json!({"argv": ["git", "push", "--force"]})).is_none());
        assert!(exact.check("process.run", &json!({"argv": ["gitk"]})).is_some(), "no implicit prefix");
        // No grant at all: denied (default-deny is the whole point).
        assert!(Grants::default().check("process.run", &json!({"argv": ["git", "status"]})).is_some());
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
